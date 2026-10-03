# Final Custom Pay Item Rate Architecture and Implementation Plan

> **Status:** Approved for implementation planning  
> **Codebase head:** Alembic migration `0042`  
> **Expected production custom-item data:** Zero. Must be confirmed by Phase 0 audit before implementation.  
> **Do not modify code, schema, or migrations until each phase is explicitly authorized.**

---

# PART ONE — Proposed Direction

## Why the Current Design Must Change

The existing system models every rate name inside a custom Pay Item as a separate, independent RateType. For example, a "Loads Delivered" item with three slot names creates three database rows in the `RateTypes` table (`CPI_42_1`, `CPI_42_2`, `CPI_42_3`) and three rows in `PayItemRateTypeMap`. Each slot appears as a separate group in the Pay Rates matrix, as if it were a completely independent rate category.

This is wrong for two reasons.

First, the calculation engine ignores slots 2 and 3 entirely. When computing a driver's pay for a tiered item, the engine queries `DriverRates` joined to `RateTypes` on `ratecode` string, using only the primary ratecode (`CPI_42_1`). The monetary amounts entered against `CPI_42_2` and `CPI_42_3` in the Pay Rates screen are stored in the database but never read during payroll calculation. They are dead data that mislead users and administrators.

Second, the model breaks the product concept. Rates for the same Pay Item and driver belong to one atomic, effective-dated, approved configuration. The existing model creates three separate `DriverRates` rows—one per RateType—with nothing enforcing that they share the same effective date, the same approval, or the same version lifecycle. A user could have Slot 1 approved with one effective date and Slot 2 still pending, leaving the system in an undefined state.

The proposed change corrects both problems by:
- Moving slot structure into the Pay Item itself (a new `PayItemRateSlots` table).
- Moving slot monetary values into one atomic parent Driver Rate version (a new `DriverRateSlotValues` table).
- Eliminating the creation of `CPI_` RateTypes for custom items entirely.
- Reusing the existing `DriverRates` row as the effective-dated, approval-lifecycle parent for all slot values.

---

## One Payroll Entry Column Per Pay Item

Every custom Daily Pay Item creates exactly one input column in the Payroll Entry grid. The payroll user enters one value—a quantity, hours, or count—into that column.

Examples:
- **Miles** → one Miles column. The driver enters 450.
- **Loads Delivered** → one Loads column. The driver enters 4.
- **Night Hours** → one Night Hours column. The driver enters 6.5.

This is true regardless of how many rate slots the item has. A "Loads Delivered" item with three ordinal tiers still produces one column. The driver enters one number. The system calculates the total payment using the rate structure behind that one column.

The Pay Rates matrix and the Payroll Entry grid are different concerns. The Pay Rates matrix shows how the item is priced for each driver. Payroll Entry shows what the driver did on a given day. The number of pricing tiers does not change the number of Payroll Entry columns.

---

## Pay Item Owns Calculation Structure

A custom Pay Item defines the structural identity of a payroll column. It stores:
- The item name and the payroll column label.
- The value type (time/hours, quantity/number).
- The calculation method (PerUnit, OrdinalTier, RangeBracket, RangeProgressive, Block).
- The list of required rate slots, in order, with names and any range boundaries.
- Whether the final slot of a range method is open-ended (catches all values above its lower bound).
- For Block: the block size and rounding rule, which are structural parameters shared across all drivers for that item.

The Pay Item does not store any driver-specific monetary amounts. It is a reusable template that tells the system how to calculate, not what to pay.

---

## Driver Pay Rate Owns Money and Version History

A Driver Pay Rate is one complete, effective-dated rate configuration for a specific driver on a specific Pay Item. It contains:
- Company, Branch, Driver, and Pay Item identity.
- An effective date range (EffectiveFrom, optional EffectiveTo).
- A lifecycle status: PendingApproval → Approved → Superseded/Voided.
- One monetary amount for every required slot defined by the Pay Item.

All slot values share the same parent. They cannot have different effective dates, different approval states, or different version histories. When the parent is approved, all its slot values are approved. When the parent is superseded, all its slot values are superseded together.

### Changing One Slot Amount Creates a Complete New Version

Suppose the current approved "Loads Delivered" rate for a driver is:
- First Load Rate: $35.00
- Second Load Rate: $30.00
- Third and Later Load Rate: $25.00
- Effective From: 2026-07-01

The driver negotiates a higher rate for third and later loads, effective 2026-08-01. The system creates a new PendingApproval version containing all three values:
- First Load Rate: $35.00 (copied forward unchanged)
- Second Load Rate: $30.00 (copied forward unchanged)
- Third and Later Load Rate: $27.00 (changed)
- Effective From: 2026-08-01

When this new version is approved, the old version is automatically closed with EffectiveTo = 2026-07-31. The system never has two Approved versions for the same driver and item with overlapping date ranges.

---

## Custom Slots Are Not RateTypes

In the existing system, "First Load Rate," "Second Load Rate," and "Third and Later Load Rate" are modeled as three independent RateTypes (`CPI_42_1`, `CPI_42_2`, `CPI_42_3`). This model implies they are independent rate categories with separate lifecycles—which is incorrect.

In the new system:
- "First Load Rate," "Second Load Rate," and "Third and Later Load Rate" are **structural slot definitions** inside the "Loads Delivered" Pay Item.
- They share one Pay Item, one Driver Rate version, one approval, and one effective date.
- They do not create RateTypes, PayItemRateTypeMap rows, or `rate_name_N` PayItemSettings entries.

**System and default Pay Items are not affected.** Items like HOURLY, MILEAGE, and LOAD continue using the existing RateType model exactly as today. The new slot-based path applies only to new custom Pay Items marked `IsSlotBased = TRUE`.

---

## Calculation Method Behavior

### PerUnit
The driver enters a quantity. The result is:
> `quantity × slot amount`

One Payroll Entry column. One structural rate slot. One driver-specific monetary amount per Driver Rate version.

### OrdinalTier
The driver enters a count (whole number). Each unit in the count maps to an ordinal position. The slot whose `SlotIndex` matches that position determines the rate for that unit. The slot with the highest `SlotIndex` catches all ordinal positions at or above it—no open-ended flag is needed or used.

Example for 4 loads with three tiers:
- Load 1 → Slot 1 (First Load Rate: $35) → $35
- Load 2 → Slot 2 (Second Load Rate: $30) → $30
- Load 3 → Slot 3 (Third and Later: $25) → $25
- Load 4 → Slot 3 (Third and Later: $25) → $25
- **Total: $115**

Decimal quantities are rejected at entry. Only whole numbers are valid for OrdinalTier.

### RangeBracket
The driver enters a quantity (may be decimal). The system finds which range bracket contains the quantity. The rate for that entire bracket is applied to the full quantity.

Using half-open intervals `[from, to)`:
- Bracket 1: `[0, 10)` at $1.00 per unit
- Bracket 2: `[10, 20)` at $2.00 per unit
- Bracket 3: `[20, ∞)` at $3.00 per unit

For quantity = 15: bracket 2 applies → 15 × $2.00 = $30.00.  
For quantity = 10: bracket 2 applies (10 is not less than 10, so it falls in `[10, 20)`).

### RangeProgressive
The driver enters a quantity. Each portion of the quantity is paid at the rate for its range.

Using the same brackets:
- First 10 units: $1.00 each → $10.00
- Next 10 units: $2.00 each → $20.00
- Any remaining units: $3.00 each

For quantity = 25: $10 + $20 + $15 = $45.00.

### Block
The driver enters a quantity. The system divides the quantity by the block size (defined on the Pay Item), rounds using the Pay Item's rounding rule, and multiplies by the per-block amount (the driver's single slot value).

Rounding rules: Floor, Ceiling, NearestHalfUp.

For quantity = 7, block size = 5, rounding = Floor, amount = $100:
> floor(7 / 5) = 1 block → $100.00

BlockSize and RoundingRule belong to the Pay Item. They are structural. All drivers for the same Pay Item use the same BlockSize and RoundingRule. Only the monetary amount per block differs by driver.

---

## Review and Finalization Trust Model

### Period Status Flow (from repository)

The actual period status transitions observed in `app/payroll/service.py`:

```
Open → InReview → Approved → Locked → Archived
```

- `_WRITE_BLOCKED_STATUSES = {"Draft", "Approved", "Locked", "Archived", "Cancelled"}` — draft line writes are blocked in these states.
- `ENTRY_ALLOWED_STATUSES = {"Open", "InReview"}` — draft lines may be created or edited only in these states.
- Finalization (`finalize_period`) transitions `Approved → Locked`. It requires `period.status == "Approved"`.
- `Locked → Archived` uses the `payroll.finalize` permission gate.

### Frozen States for Calculation Evidence

**Calculation evidence is frozen** (no recalculation permitted) in: **`InReview`** and **`Approved`**.

- `Open`: calculations run freely.
- `InReview`: calculations frozen. Rate changes mark affected lines stale.
- `Approved`: calculations frozen. Rate changes mark affected lines stale. Draft line entry is write-blocked by the existing guard.
- `Locked` / `Archived`: period is fully immutable. No draft lines exist.

The frozen-state constant used throughout the plan:
```python
_FROZEN_CALCULATION_STATUSES = frozenset({'InReview', 'Approved'})
```

### Open
Calculations run freely. When the payroll user enters or changes a quantity, the system calculates using the currently approved rate for the driver. If rates are updated during Open, draft calculations refresh.

### Open → InReview Transition
The system runs one final `_refresh_draft_calculations`. For every non-void draft line, it:
1. Resolves the effective Driver Rate version for the driver/branch/item/date.
2. Calculates the amount.
3. Produces the `CalculationBreakdown`.
4. Writes all evidence fields atomically: `CalculatedAmount`, `ResolvedDriverRateID`, `CalculationBreakdown`, `CalculatedAtUtc`, and sets `IsStale = FALSE`.

After this point, the draft line holds the evidence of what was calculated and which rate version was used.

### InReview
Calculations are frozen. The system must not silently recalculate. The evidence on the draft line is what the manager sees and reviews.

### InReview → Approved Transition
No recalculation. The reviewed evidence is carried forward as-is. Any stale lines must have been resolved before this transition is permitted.

### Approved
Calculations remain frozen. Draft entry is write-blocked. Finalization (`Approved → Locked`) reads the stored evidence directly without re-resolving rates.

If a new rate is approved or voided while the period is in `InReview` or `Approved` status:
- The rate function detects draft lines in frozen-status periods whose `ResolvedDriverRateID` references the rate being superseded or voided.
- It sets `IsStale = TRUE` on those lines.
- The period cannot finalize while any non-void line is stale.

To use the new rate:
1. Return the period to Open.
2. Run `_refresh_draft_calculations`.
3. Transition to InReview again.
4. The manager reviews the new amounts.
5. Finalization proceeds.

### Finalization
Finalization **validates** the stored evidence. It does not run a new rate resolution query, does not call `_refresh_draft_calculations`, and does not produce a different amount.

Pre-finalization checks:
1. No non-void draft lines have `IsStale = TRUE`.
2. All rate-requiring non-void lines have `CalculatedAmount IS NOT NULL`.
3. All rate-requiring non-void lines have `ResolvedDriverRateID IS NOT NULL`.
4. All referenced `ResolvedDriverRateID` values have status `Approved` or `Superseded` (not Voided).
5. All non-void lines with a resolved rate have `CalculationBreakdown IS NOT NULL`.

The finalized amount is the stored `CalculatedAmount`. The finalized snapshot contains the stored `CalculationBreakdown`. The finalized `DriverRateID` is the stored `ResolvedDriverRateID`. None of these are re-derived at finalization time.

---

# PART TWO — Complete Technical Plan

---

## Section 1 — Current Application Behavior

**Files inspected:**
- `backend/app/settings/service.py` — `create_custom_pay_item`, `approve_rate`, `copy_driver_rates`, `batch_save_rates`
- `backend/app/payroll/service.py` — `_compute_calculated_amount`, `_compute_ordinal_tier`, `_compute_range_bracket`, `_compute_range_progressive`, `_compute_block`, `_compute_per_unit`, `_resolve_rate_behavior`, `_refresh_draft_calculations`, `finalize_period`, finalization INSERT SELECT, `change_period_status`, `_WRITE_BLOCKED_STATUSES`, `ENTRY_ALLOWED_STATUSES`
- `backend/app/payroll/schemas.py` — `DriverRateCreate`, `DriverRateUpdate`
- `backend/app/settings/schemas.py` — `CustomPayItemCreate`
- `backend/alembic/versions/` — migrations 0001–0042
- `backend/tests/test_m13c.py`, `test_settings_custom_pay_items.py`, `test_pay_rates.py`

### How Custom Pay Items Currently Create RateTypes

`create_custom_pay_item` in `app/settings/service.py` iterates over `effective_rate_names` (1-indexed). For each name at index `idx`, it executes three INSERTs:

1. **`payroll.payitemsettings`**: `settingkey = 'rate_name_{idx}'`, `settingvalutext = rname`
2. **`payroll.ratetypes`**: `ratecode = f'CPI_{new_id}_{idx}'`, ON CONFLICT DO UPDATE
3. **`payroll.payitemratetypemap`**: `isprimary = (idx == 1)`, ON CONFLICT DO NOTHING

`effective_rate_names` is determined by:
- `_MULTI_RATE_BEHAVIORS = {'OrdinalTier', 'RangeBracket', 'RangeProgressive', 'Block'}`: defaults to `["Rate 1", "Rate 2", "Rate 3"]` if no names supplied → **3 RateTypes created**.
- `PerUnit` with no names supplied: defaults to `["{item_name} Rate"]` → **1 RateType created**.
- Caller-supplied `rate_names`: exactly N RateTypes created.

No DriverRates or DriverRateTiers rows are created at item creation time.

### How Many RateTypes Each Method Creates (Defaults)

| Method | Default RateType count | Codes created |
|---|---|---|
| PerUnit | 1 | `CPI_{id}_1` |
| OrdinalTier | 3 | `CPI_{id}_1`, `CPI_{id}_2`, `CPI_{id}_3` |
| RangeBracket | 3 | `CPI_{id}_1`, `CPI_{id}_2`, `CPI_{id}_3` |
| RangeProgressive | 3 | `CPI_{id}_1`, `CPI_{id}_2`, `CPI_{id}_3` |
| Block | 3 | `CPI_{id}_1`, `CPI_{id}_2`, `CPI_{id}_3` |

### How the Calculation Engine Chooses the Primary RateType

The Python-side helpers (`_compute_per_unit`, `_compute_ordinal_tier`, etc.) resolve a rate by querying `DriverRates` joined to `RateTypes` on `ratecode` string:

```sql
SELECT dr.driverrateid, dr.ratetypeid, dr.amount
FROM   payroll.driverrates dr
JOIN   payroll.ratetypes   rt ON rt.ratetypeid = dr.ratetypeid
WHERE  dr.driverid       = :did
  AND  dr.companyid      = :cid
  AND  rt.ratecode       = :rcode          -- primary ratecode only
  AND  dr.status         IN ('Approved', 'Superseded')
  AND  dr.effectivefrom <= :dt
  AND  (dr.effectiveto IS NULL OR dr.effectiveto >= :dt)
ORDER BY dr.effectivefrom DESC
LIMIT 1
```

`rcode` here is the primary RateType's `ratecode` (e.g., `CPI_42_1`). Secondary rate codes (`CPI_42_2`, `CPI_42_3`) are **never queried** by the calculation engine.

The finalization LATERAL resolves differently—via `PayItemRateTypeMap` filtered by `payitemid`, which can match any of the N map rows. However, because LIMIT 1 without ORDER BY on `isprimary` is used, the specific row returned is not guaranteed to be the primary one. This is a confirmed bug in the existing architecture that the new model eliminates.

### Whether Secondary RateTypes Are Ignored

**Confirmed: yes.** The secondary `CPI_` RateTypes (`CPI_42_2`, `CPI_42_3`) appear in the Pay Rates matrix (one group per PayItemRateTypeMap row), allowing users to enter driver amounts against them. Those amounts are stored in separate `DriverRates` rows but are never read during calculation. They are dead data.

### How DriverRateTiers Currently Work

`payroll.DriverRateTiers` is a child table of `DriverRates`. Schema (from migration 0009):

```
DriverRateTierID  SERIAL        PK
DriverRateID      INTEGER NOT NULL FK → DriverRates ON DELETE CASCADE
TierSequence      INTEGER NOT NULL CHECK > 0
FromUnit          NUMERIC(18,4) NOT NULL CHECK >= 0
ToUnit            NUMERIC(18,4) NULL  -- NULL = open-ended last tier
TierAmount        NUMERIC(18,4) NOT NULL CHECK > 0
UNIQUE (DriverRateID, TierSequence)
```

`ToUnit = NULL` signals the open-ended final tier. The current convention uses **right-closed** intervals: `FromUnit <= qty <= ToUnit`. This is the existing behavior for system items using `DriverRateTiers` and will remain unchanged. The new `PayItemRateSlots` table adopts a **half-open** `[FromUnit, ToUnit)` convention (see Section 3).

Tiers are protected by `trg_guard_driverratetier_used_mutation`: once the parent DriverRate is referenced in `PayrollFinalLines`, tier rows cannot be mutated.

### How the Pay Rates Matrix Is Currently Returned

`get_driver_rate_matrix` in `app/payroll/service.py` queries:

```sql
SELECT pi.payitemid, pi.payitemname, pi.ratebehavior,
       rt.ratetypeid, rt.ratecode, rt.ratename, rt.unitname
FROM   payroll.payitems pi
JOIN   payroll.payitemratetypemap pirm ON pirm.payitemid = pi.payitemid AND pirm.status = 'Active'
JOIN   payroll.ratetypes rt ON rt.ratetypeid = pirm.ratetypeid AND rt.isactive = TRUE
...
```

A custom item with N PayItemRateTypeMap rows produces **N groups** in the matrix response—one per RateType. The Pay Rates UI shows N separate rate-entry fields for one logical Pay Item. This contradicts the product model of one item = one group.

### How Finalization Currently Resolves and Snapshots Rates

The finalization INSERT SELECT in `finalize_period` uses a LATERAL join called `rate_sub` that resolves via `PayItemRateTypeMap.payitemid → ratetypes → driverrates`. The LATERAL does not reliably select the primary RateType because it lacks `ORDER BY isprimary DESC` before LIMIT 1. For slot-based items in the new model, this LATERAL will be replaced (see Section 12).

The `SourceSnapshot` JSONB currently contains: `pay_item_id`, `pay_item_code`, `pay_item_name`, `rate_behavior`, `rate_type_id`, `rate_type_code`, `rate_type_name`, `driver_rate_id`, `driver_rate_amount`, `driver_rate_status`, `driver_rate_eff_from`, `driver_rate_eff_to`, `block_size`, `rounding_rule`, `tiers` (array), `finalized_at`.

`ResolvedRateAmount` on `PayrollFinalLines` is set to `dr.Amount` for PerUnit items and NULL for tiered items.

### Where `_refresh_draft_calculations` Is Currently Called

**Confirmed current locations:**
1. During the **Open → InReview transition** (before the period enters InReview status).
2. At the **start of `finalize_period`** (this is the bug described in the freeze model).

The plan removes call #2 from `finalize_period`. Finalization will validate stored values instead.

### Current Period Status Flow (Confirmed from Repository)

```python
# From app/payroll/service.py
_WRITE_BLOCKED_STATUSES = {"Draft", "Approved", "Locked", "Archived", "Cancelled"}
ENTRY_ALLOWED_STATUSES  = {"Open", "InReview"}  # from schemas.py

# Status transition permission gates (from _TRANSITION_GATES):
# ("Open",   "InReview"):  "payroll.entry"
# ("Locked", "Archived"):  "payroll.finalize"
# InReview → Approved:     via review decision (M16)
# Approved → Locked:       via finalize_period (requires status == "Approved")
```

Real flow: `Open → InReview → Approved → Locked → Archived`

---

## Section 2 — Final Entity Model

```
PayItems (system or custom)
  │
  ├── [system items] PayItemRateTypeMap → RateTypes
  │       (existing path, unchanged)
  │
  └── [custom slot-based items] PayItemRateSlots
          structural definitions: slot order, names, boundaries, open-ended flag
          no monetary data

DriverRates (one parent version per company+branch+driver+item+effectivefrom)
  │
  ├── [system items] .RateTypeID is set, .PayItemID is NULL, .Amount is populated
  │       DriverRateTiers (child tier rows for system tiered behaviors)
  │
  └── [custom slot-based items] .PayItemID is set, .RateTypeID is NULL, .Amount is NULL
          DriverRateSlotValues (child slot-value rows, one per PayItemRateSlot)

PayrollDraftLines
  └── ResolvedDriverRateID FK → DriverRates (the version used for CalculatedAmount)
       CalculationBreakdown JSONB (full per-slot or per-tier evidence — all rate paths)
       CalculatedAtUtc, IsStale

PayrollFinalLines
  └── DriverRateID FK → DriverRates (= ResolvedDriverRateID from draft line)
       SourceSnapshot JSONB (contains CalculationBreakdown + stable metadata)
       RateTypeID NULL for custom items
       ResolvedRateAmount NULL for multi-slot items

BranchPayItemConfig
  └── Controls which Pay Items are active for which branch (unchanged)
```

### Entity Responsibilities

| Entity | Owns |
|---|---|
| `PayItems` | Item identity, calculation method, structural parameters (BlockSize, RoundingRule for slot-based Block items), IsSlotBased flag |
| `PayItemRateSlots` | Structural slot definitions for custom items: order, names, range boundaries, open-ended flag |
| `RateTypes` | System rate category catalog (HOURLY, MILEAGE, etc.); company-owned CPI_ codes retained but deactivated |
| `PayItemRateTypeMap` | System item ↔ RateType linking (unchanged); no new custom rows written |
| `DriverRates` | Effective-dated version parent: identity, dates, status, approval, audit |
| `DriverRateSlotValues` | Monetary amounts for each slot in a custom Driver Rate version |
| `DriverRateTiers` | Tier boundaries and amounts for system tiered rate items (unchanged) |
| `PayrollDraftLines` | Working payroll data + calculation evidence for all rate paths: CalculatedAmount, ResolvedDriverRateID, CalculationBreakdown, IsStale |
| `PayrollFinalLines` | Immutable finalized record + SourceSnapshot |
| `BranchPayItemConfig` | Branch activation of Pay Items (unchanged) |

---

## Section 3 — Exact Proposed Database Schema

### 3.1 `payroll.PayItemRateSlots`

```sql
CREATE TABLE payroll.PayItemRateSlots (
    PayItemRateSlotID  SERIAL          PRIMARY KEY,
    PayItemID          INTEGER         NOT NULL
                                       REFERENCES payroll.PayItems(PayItemID),
    SlotIndex          SMALLINT        NOT NULL
                                       CHECK (SlotIndex > 0),
    SlotName           VARCHAR(100)    NOT NULL
                                       CHECK (LENGTH(TRIM(SlotName)) > 0),
    FromUnit           NUMERIC(18,4)   NULL
                                       CHECK (FromUnit IS NULL OR FromUnit >= 0),
    ToUnit             NUMERIC(18,4)   NULL,
    IsOpenEnded        BOOLEAN         NOT NULL DEFAULT FALSE,
    Status             VARCHAR(20)     NOT NULL DEFAULT 'Active'
                                       CHECK (Status IN ('Active', 'Retired')),
    CreatedByUserID    INTEGER,
    CreatedAtUtc       TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

-- No duplicate slot index per item
ALTER TABLE payroll.PayItemRateSlots
    ADD CONSTRAINT uq_PayItemRateSlots_Item_Index
    UNIQUE (PayItemID, SlotIndex);

-- IsOpenEnded = TRUE is only valid when ToUnit is NULL.
-- This constraint does NOT enforce IsOpenEnded = TRUE whenever ToUnit IS NULL,
-- because PerUnit, Block, and OrdinalTier slots have ToUnit = NULL and IsOpenEnded = FALSE.
-- Method-specific rules are enforced by service-layer validation (see below).
ALTER TABLE payroll.PayItemRateSlots
    ADD CONSTRAINT ck_PayItemRateSlots_OpenEnded
    CHECK (NOT IsOpenEnded OR ToUnit IS NULL);

-- If ToUnit is set, it must be strictly greater than FromUnit
ALTER TABLE payroll.PayItemRateSlots
    ADD CONSTRAINT ck_PayItemRateSlots_Bounds
    CHECK (ToUnit IS NULL OR (FromUnit IS NOT NULL AND ToUnit > FromUnit));

-- Lookup index
CREATE INDEX ix_PayItemRateSlots_PayItemID
    ON payroll.PayItemRateSlots (PayItemID)
    WHERE Status = 'Active';
```

**No `CalculationRole` column.** The calculation method is fully defined by `PayItems.RateBehavior`. Adding a redundant column that must stay synchronized with the parent is unnecessary complexity.

#### Service-Layer Slot Field Validation (Enforced Per Method)

The database constraint `ck_PayItemRateSlots_OpenEnded` only asserts that `IsOpenEnded = TRUE` cannot coexist with a non-NULL `ToUnit`. The stricter per-method rules are enforced by service-layer validation in `create_custom_pay_item` and `_validate_slot_definitions`:

| Method | FromUnit | ToUnit | IsOpenEnded |
|---|---|---|---|
| PerUnit (all slots) | NULL | NULL | FALSE |
| OrdinalTier (all slots) | NULL | NULL | FALSE |
| Block (all slots) | NULL | NULL | FALSE |
| RangeBracket / RangeProgressive — intermediate slot | populated | populated | FALSE |
| RangeBracket / RangeProgressive — final slot | populated | NULL | TRUE |

For OrdinalTier: `IsOpenEnded = FALSE` on every slot without exception. The calculation engine applies the highest-`SlotIndex` slot's rate to all ordinal positions at or above that index — no open-ended flag is read or required.

**Range interval semantics — `[FromUnit, ToUnit)`:**

| Slot | FromUnit | ToUnit | IsOpenEnded | Meaning |
|---|---|---|---|---|
| 1 | 0 | 10 | FALSE | `0 ≤ qty < 10` |
| 2 | 10 | 20 | FALSE | `10 ≤ qty < 20` |
| 3 | 20 | NULL | TRUE | `20 ≤ qty < ∞` |

`ToUnit` is the **exclusive** upper bound. For the open-ended final slot of a range method, `ToUnit = NULL` and `IsOpenEnded = TRUE`.

**Status field purpose:** Allows retiring a slot before any rate versions exist. Once any non-Voided DriverRate references the Pay Item, the slot cannot be retired (trigger enforces this). The `Status` column supports future Pay Item lifecycle management without requiring deletion.

---

### 3.2 `payroll.DriverRateSlotValues`

```sql
CREATE TABLE payroll.DriverRateSlotValues (
    DriverRateSlotValueID  SERIAL         PRIMARY KEY,
    DriverRateID           INTEGER        NOT NULL
                                          REFERENCES payroll.DriverRates(DriverRateID)
                                          ON DELETE CASCADE,
    PayItemRateSlotID      INTEGER        NOT NULL
                                          REFERENCES payroll.PayItemRateSlots(PayItemRateSlotID)
                                          ON DELETE RESTRICT,
    Amount                 NUMERIC(18,4)  NOT NULL
                                          CHECK (Amount > 0)
);

-- Each slot appears exactly once per parent version
ALTER TABLE payroll.DriverRateSlotValues
    ADD CONSTRAINT uq_DriverRateSlotValues_Rate_Slot
    UNIQUE (DriverRateID, PayItemRateSlotID);

-- Lookup by parent version
CREATE INDEX ix_DriverRateSlotValues_DriverRateID
    ON payroll.DriverRateSlotValues (DriverRateID);
```

**Confirmed: no separate status, no effective dates, no approval fields on child rows.** The entire lifecycle belongs to the parent `DriverRates` row. Voiding, superseding, or approving the parent automatically applies to all its slot values.

#### Deletion Behavior (Authoritative)

**`DriverRateID` FK — `ON DELETE CASCADE`:** If the parent `DriverRates` row is physically deleted, all its `DriverRateSlotValues` are automatically deleted. Physical deletion of a `DriverRates` row is only permitted for rows that have never been referenced in `PayrollFinalLines`, which is enforced by `trg_guard_driverrate_used_mutation`. Voiding and superseding are status changes on the parent—child rows are preserved and the CASCADE does not fire.

**`PayItemRateSlotID` FK — `ON DELETE RESTRICT`:** A `PayItemRateSlots` row cannot be physically deleted while any `DriverRateSlotValues` row references it. The structural mutation trigger (`trg_guard_payitemrateslot_structural_mutation`) additionally prevents deletion of any slot while the Pay Item has active rate versions or payroll usage. These two guards are independent and complementary: the trigger blocks deletion when the item is in use; the FK RESTRICT prevents any physical deletion that would leave orphaned slot values regardless of trigger execution order.

**Finalized-history mutation protection:** A trigger `trg_guard_driverrateslotvalue_used_mutation` (BEFORE UPDATE OR DELETE) checks `EXISTS (SELECT 1 FROM payroll.PayrollFinalLines WHERE DriverRateID = OLD.DriverRateID)`. If true, it raises an exception. This mirrors the existing `trg_guard_driverratetier_used_mutation`.

---

### 3.3 Changes to `payroll.DriverRates`

Current state:
- `RateTypeID INTEGER NOT NULL`
- `Amount NUMERIC(18,4) NOT NULL`
- `BlockSize NUMERIC(18,4) NULL` (added by migration 0009)
- `RoundingRule VARCHAR(20) NULL` (added by migration 0009)

Required changes:

```sql
-- 1. Add PayItemID column
ALTER TABLE payroll.DriverRates
    ADD COLUMN PayItemID INTEGER NULL
        REFERENCES payroll.PayItems(PayItemID);

-- 2. Drop NOT NULL from RateTypeID
--    MUST precede the XOR check constraint
ALTER TABLE payroll.DriverRates
    ALTER COLUMN RateTypeID DROP NOT NULL;

-- 3. Make Amount nullable
ALTER TABLE payroll.DriverRates
    ALTER COLUMN Amount DROP NOT NULL;

-- 4. Exactly one of RateTypeID or PayItemID must be set
ALTER TABLE payroll.DriverRates
    ADD CONSTRAINT ck_DriverRates_ItemOrType
    CHECK (
        (RateTypeID IS NOT NULL AND PayItemID IS NULL)
        OR
        (RateTypeID IS NULL     AND PayItemID IS NOT NULL)
    );

-- 5. Amount required for system rows; NULL for slot-based rows
ALTER TABLE payroll.DriverRates
    ADD CONSTRAINT ck_DriverRates_Amount_Presence
    CHECK (
        (PayItemID IS NULL AND Amount IS NOT NULL AND Amount > 0)
        OR
        (PayItemID IS NOT NULL AND Amount IS NULL)
    );

-- 6. BlockSize/RoundingRule must be NULL on slot-based rows
--    (Block config lives on PayItems for custom items)
ALTER TABLE payroll.DriverRates
    ADD CONSTRAINT ck_DriverRates_Block_Slot_Exclusion
    CHECK (
        PayItemID IS NULL
        OR (BlockSize IS NULL AND RoundingRule IS NULL)
    );
```

**All existing rows are valid after these changes.** Every existing row has `RateTypeID IS NOT NULL` and `PayItemID IS NULL`, satisfying `ck_DriverRates_ItemOrType`. Every existing row has `Amount IS NOT NULL` and `PayItemID IS NULL`, satisfying `ck_DriverRates_Amount_Presence`.

**`BlockSize` and `RoundingRule` columns remain on `DriverRates`** for system rows that use Block behavior (if any). The `ck_DriverRates_Block_Slot_Exclusion` constraint prevents them from being set on custom slot-based rows.

---

### 3.4 Changes to `payroll.PayItems`

```sql
ALTER TABLE payroll.PayItems
    ADD COLUMN IsSlotBased  BOOLEAN       NOT NULL DEFAULT FALSE,
    ADD COLUMN BlockSize    NUMERIC(18,4) NULL,
    ADD COLUMN RoundingRule VARCHAR(20)   NULL;

-- Block config constraint — scoped to slot-based items only.
-- IsSlotBased = FALSE rows (all existing system and legacy items) always satisfy
-- this constraint regardless of their BlockSize/RoundingRule values on DriverRates.
ALTER TABLE payroll.PayItems
    ADD CONSTRAINT ck_PayItems_BlockConfig
    CHECK (
        IsSlotBased = FALSE
        OR (
            (
                RateBehavior = 'Block'
                AND BlockSize    IS NOT NULL
                AND BlockSize    > 0
                AND RoundingRule IS NOT NULL
                AND RoundingRule IN ('Floor', 'Ceiling', 'NearestHalfUp')
            )
            OR
            (
                RateBehavior != 'Block'
                AND BlockSize    IS NULL
                AND RoundingRule IS NULL
            )
        )
    );
```

`IsSlotBased = TRUE` is set at item creation time and is never changed after that. It is the authoritative dispatch flag for the calculation engine, API routing, and finalization.

All existing rows have `IsSlotBased = FALSE` (default). System items are permanently `IsSlotBased = FALSE`. Because the constraint short-circuits on `IsSlotBased = FALSE`, existing system items with `BlockSize` and `RoundingRule` stored on their `DriverRates` rows are unaffected — the constraint makes no assertion about `PayItems.BlockSize` or `PayItems.RoundingRule` when `IsSlotBased = FALSE`.

---

### 3.5 Changes to `payroll.PayrollDraftLines`

```sql
ALTER TABLE payroll.PayrollDraftLines
    ADD COLUMN ResolvedDriverRateID  INTEGER      NULL
        REFERENCES payroll.DriverRates(DriverRateID),
    ADD COLUMN CalculationBreakdown  JSONB        NULL,
    ADD COLUMN CalculatedAtUtc       TIMESTAMPTZ  NULL,
    ADD COLUMN IsStale               BOOLEAN      NOT NULL DEFAULT FALSE;

-- For staleness detection in approve_rate / void_rate
CREATE INDEX ix_PayrollDraftLines_ResolvedDriverRateID
    ON payroll.PayrollDraftLines (ResolvedDriverRateID)
    WHERE ResolvedDriverRateID IS NOT NULL AND IsStale = FALSE;

-- For pre-finalization stale check
CREATE INDEX ix_PayrollDraftLines_Stale
    ON payroll.PayrollDraftLines (PayrollPeriodID, IsStale)
    WHERE IsStale = TRUE;
```

---

## Section 4 — Branch-Scoped Effective-Dating Identity

The approved identity for a custom Driver Rate version is:

```
(CompanyID, BranchID, DriverID, PayItemID, effective date range)
```

A rate registered for Branch A must not resolve for a payroll line belonging to Branch B. The payroll line's `BranchID` is used in all rate resolution queries.

### Constraints and Indexes

```sql
-- GIST exclusion: no overlapping Approved/Superseded ranges per branch-scoped identity
ALTER TABLE payroll.DriverRates
    ADD CONSTRAINT excl_DriverRates_PayItem_no_overlap
    EXCLUDE USING gist (
        CompanyID   WITH =,
        BranchID    WITH =,
        DriverID    WITH =,
        PayItemID   WITH =,
        daterange(EffectiveFrom, COALESCE(EffectiveTo, '9999-12-31'::date), '[]') WITH &&
    )
    WHERE (PayItemID IS NOT NULL AND Status IN ('Approved', 'Superseded'));

-- At most one Approved version per branch-scoped identity
CREATE UNIQUE INDEX ux_DriverRates_Driver_PayItem_Approved
    ON payroll.DriverRates (CompanyID, BranchID, DriverID, PayItemID)
    WHERE PayItemID IS NOT NULL AND Status = 'Approved';

-- At most one PendingApproval version per branch-scoped identity
CREATE UNIQUE INDEX ux_DriverRates_Driver_PayItem_Pending
    ON payroll.DriverRates (CompanyID, BranchID, DriverID, PayItemID)
    WHERE PayItemID IS NOT NULL AND Status = 'PendingApproval';

-- Lookup index for rate resolution
CREATE INDEX ix_DriverRates_Driver_PayItem_Date
    ON payroll.DriverRates (CompanyID, BranchID, DriverID, PayItemID, EffectiveFrom DESC)
    WHERE PayItemID IS NOT NULL AND Status IN ('Approved', 'Superseded');
```

### Impact on Each Operation

**Lookup (calculation and finalization):**
```sql
WHERE CompanyID     = :company_id
  AND BranchID      = :branch_id      -- from the payroll draft line
  AND DriverID      = :driver_id
  AND PayItemID     = :pay_item_id
  AND Status        IN ('Approved', 'Superseded')
  AND EffectiveFrom <= :work_date
  AND (EffectiveTo IS NULL OR EffectiveTo >= :work_date)
ORDER BY EffectiveFrom DESC
LIMIT 1
```

**Create:** `BranchID` is a required field. Validated against the driver's active branch membership.

**Update pending:** Only the matching pending version for the same `(CompanyID, BranchID, DriverID, PayItemID)` may be edited.

**Approve:** Supersession query filters on `(CompanyID, BranchID, DriverID, PayItemID)`. Advisory lock is already keyed on `(company_id, branch_id)` — no change needed.

**Supersede:** `_supersede_current_approved_rates` adds `BranchID` to its WHERE clause.

**Void:** Voiding is a status change on the parent row. BranchID is already on the row. No query change.

**Copy:** Target must specify `branch_id`. Cross-company copies are rejected. Cross-branch copies create an independent pending version with the target `BranchID` — they do not inherit the source branch or its date ranges.

**History:** Filtered by `(CompanyID, BranchID, DriverID, PayItemID)` — each branch has its own independent version history.

**Finalized-period protection:** `_check_not_in_finalized_period` already filters by `(CompanyID, BranchID, PayrollPeriodID)` — no change needed.

**BranchID is not part of the rate identity for system items** — this is intentional and unchanged. System items continue without BranchID in their rate resolution path.

---

## Section 5 — Approval, Supersession, Voiding, and Copying

### Create

**Request must contain the complete slot set.** No partial saves.

Validations (in order):
1. Company matches the authenticated user's company.
2. Branch is valid and the driver is associated with it.
3. Pay Item exists, is Active, and has `IsSlotBased = TRUE`.
4. Load all active `PayItemRateSlots` for the item. Call this the required set.
5. Incoming slot values: count must equal the required set. Every `PayItemRateSlotID` must appear exactly once. No unknown IDs. No retired slot IDs.
6. All amounts > 0.
7. No existing `PendingApproval` version for `(CompanyID, BranchID, DriverID, PayItemID)`. If one exists, return HTTP 409 with `existing_driver_rate_id` in the response body.
8. `effective_from` is not within a finalized or locked payroll period.

If all validations pass:
- INSERT one `DriverRates` row (`PayItemID` set, `RateTypeID = NULL`, `Amount = NULL`, `Status = 'PendingApproval'`).
- INSERT all `DriverRateSlotValues` rows.
- Both inserts occur in a single database transaction.

### Update Pending

Only `Status = 'PendingApproval'` versions may be edited in place. The complete slot set replaces the existing one atomically: DELETE all existing `DriverRateSlotValues` for the `DriverRateID`, then INSERT the new set. The parent `DriverRates` row metadata (dates, notes) may also be updated. Both operations occur in one transaction.

Partial slot edits are not accepted. A request that supplies only two of three required slots is rejected.

### Approve

1. Load the parent `DriverRates` row. Verify `Status = 'PendingApproval'` and `PayItemID IS NOT NULL`.
2. Load all active `PayItemRateSlots` for the `PayItemID`.
3. Load all `DriverRateSlotValues` for this `DriverRateID`.
4. Verify: count of slot values equals count of required slots; every required slot ID is present exactly once. If not → HTTP 422 "Rate configuration is incomplete. All required slots must have an amount before approval."
5. Acquire `pg_advisory_xact_lock(company_id, branch_id)`.
6. Run `_check_not_in_finalized_period`.
7. Run `_check_no_future_approved_conflict` (using PayItemID + BranchID).
8. Run `_supersede_current_approved_rates`: set `EffectiveTo = EffectiveFrom - 1 day` on the current Approved version for the same `(CompanyID, BranchID, DriverID, PayItemID)` where `EffectiveFrom < new_from`.
9. `UPDATE DriverRates SET Status = 'Approved', ApprovedByUserID = :uid, ApprovedAtUtc = NOW()`.
10. Run stale detection: find draft lines in periods with `Status IN ('InReview', 'Approved')` whose `ResolvedDriverRateID` references the newly superseded rate. Set `IsStale = TRUE` on those lines (see Section 10 for full SQL).
11. Write audit record.

**No slot-level approval exists.** Approval is always of the complete parent version.

### Changing One Approved Amount

The user initiates this via the Create endpoint (or a dedicated "new version from current" UI action). The service:
1. Loads the current Approved version's slot values.
2. Creates a new PendingApproval version with all slot values copied forward.
3. Applies the user's requested change to the specified slot value.
4. Returns the new pending version ID.

The user then reviews and approves the new version through the normal approval flow.

### Void

Voiding operates on the full parent `DriverRates` row (`Status = 'Voided'`). The existing void guard prevents voiding a rate that has been referenced in finalized payroll.

After voiding, run stale detection: find draft lines in periods with `Status IN ('InReview', 'Approved')` whose `ResolvedDriverRateID` references the voided rate. Set `IsStale = TRUE` on those lines (see Section 10 for full SQL).

### Copy

Copy creates a new PendingApproval version for the target driver:
1. Load source `DriverRates` row (must have `PayItemID IS NOT NULL`).
2. Validate cross-company rejection.
3. For cross-branch copies: target branch must be explicitly specified; a new independent pending version is created for the target branch identity.
4. INSERT new `DriverRates` row with target `CompanyID`, `BranchID`, `DriverID`, `PayItemID`, new `EffectiveFrom`, `Status = 'PendingApproval'`.
5. INSERT all `DriverRateSlotValues` from the source `DriverRateID`.
6. Both in one transaction.
7. Return the new pending version. The user reviews and approves.

---

## Section 6 — API Plan

### Route Family

```
/payroll/driver-item-rates
```

These endpoints are dedicated to custom slot-based items. The existing `/payroll/rates` endpoints remain unchanged and continue serving system/default items.

### Endpoints

#### `GET /payroll/driver-item-rates/matrix`

Query params: `driver_id`, `branch_id`

Returns all active custom slot-based Pay Items for the branch, with the current Approved version (if any) and any pending version for the specified driver.

**Response example — OrdinalTier item:**
```json
{
  "items": [
    {
      "pay_item_id": 42,
      "pay_item_code": "CPI_ABCD1234",
      "pay_item_name": "Loads Delivered",
      "rate_behavior": "OrdinalTier",
      "slots": [
        { "slot_id": 1, "slot_index": 1, "slot_name": "First Load Rate",
          "from_unit": null, "to_unit": null, "is_open_ended": false },
        { "slot_id": 2, "slot_index": 2, "slot_name": "Second Load Rate",
          "from_unit": null, "to_unit": null, "is_open_ended": false },
        { "slot_id": 3, "slot_index": 3, "slot_name": "Third and Later Load Rate",
          "from_unit": null, "to_unit": null, "is_open_ended": false }
      ],
      "current_version": {
        "driver_rate_id": 201,
        "status": "Approved",
        "effective_from": "2026-01-01",
        "effective_to": null,
        "slot_values": [
          { "slot_id": 1, "slot_name": "First Load Rate", "amount": "35.0000" },
          { "slot_id": 2, "slot_name": "Second Load Rate", "amount": "30.0000" },
          { "slot_id": 3, "slot_name": "Third and Later Load Rate", "amount": "25.0000" }
        ]
      },
      "pending_version": null
    }
  ]
}
```

Note: all OrdinalTier slots have `"is_open_ended": false`. The calculation engine applies Slot 3's rate to all ordinal positions ≥ 3 without needing an open-ended flag.

#### `GET /payroll/driver-item-rates/{driver_id}/{pay_item_id}`

Query param: `branch_id`

Returns current Approved version, any pending version, and full version history for the specified driver/item/branch.

#### `POST /payroll/driver-item-rates/{driver_id}/{pay_item_id}/versions`

Create a new PendingApproval version. The request must contain the complete slot set.

**Request — PerUnit:**
```json
{
  "branch_id": 5,
  "effective_from": "2026-07-01",
  "effective_to": null,
  "notes": "Annual rate review",
  "slot_values": [
    { "slot_id": 7, "amount": "0.8500" }
  ]
}
```

**Request — OrdinalTier (three slots):**
```json
{
  "branch_id": 5,
  "effective_from": "2026-07-01",
  "slot_values": [
    { "slot_id": 1, "amount": "35.0000" },
    { "slot_id": 2, "amount": "30.0000" },
    { "slot_id": 3, "amount": "25.0000" }
  ]
}
```

**Request — RangeProgressive (three slots):**
```json
{
  "branch_id": 5,
  "effective_from": "2026-07-01",
  "slot_values": [
    { "slot_id": 10, "amount": "1.2000" },
    { "slot_id": 11, "amount": "1.5000" },
    { "slot_id": 12, "amount": "2.0000" }
  ]
}
```

**Request — RangeBracket (three slots):**
```json
{
  "branch_id": 5,
  "effective_from": "2026-07-01",
  "slot_values": [
    { "slot_id": 20, "amount": "1.0000" },
    { "slot_id": 21, "amount": "2.0000" },
    { "slot_id": 22, "amount": "3.0000" }
  ]
}
```

**Request — Block (one slot):**
```json
{
  "branch_id": 5,
  "effective_from": "2026-07-01",
  "slot_values": [
    { "slot_id": 30, "amount": "100.0000" }
  ]
}
```

Response: `201 Created` with the full version object including `driver_rate_id`.

#### `PUT /payroll/driver-item-rates/versions/{version_id}`

Update a PendingApproval version. Same request body as Create. Replaces the complete slot set atomically.

#### `POST /payroll/driver-item-rates/versions/{version_id}/approve`

No request body. Returns the approved version.

#### `POST /payroll/driver-item-rates/versions/{version_id}/void`

```json
{ "reason": "Entered in error" }
```

#### `POST /payroll/driver-item-rates/{driver_id}/{pay_item_id}/copy-from/{source_driver_id}`

```json
{
  "branch_id": 5,
  "effective_from": "2026-07-01"
}
```

Creates a PendingApproval version for the target driver with amounts copied from the source driver's current Approved version.

### Future Bulk Contract

The initial API saves one complete version per request. The future bulk endpoint follows this contract:

**Request:**
```json
{
  "effective_from": "2026-07-01",
  "versions": [
    {
      "driver_id": 101,
      "branch_id": 5,
      "pay_item_id": 42,
      "slot_values": [
        { "slot_id": 1, "amount": "35.0000" },
        { "slot_id": 2, "amount": "30.0000" },
        { "slot_id": 3, "amount": "25.0000" }
      ]
    },
    {
      "driver_id": 102,
      "branch_id": 5,
      "pay_item_id": 42,
      "slot_values": [
        { "slot_id": 1, "amount": "32.0000" },
        { "slot_id": 2, "amount": "28.0000" },
        { "slot_id": 3, "amount": "22.0000" }
      ]
    }
  ]
}
```

**Rules:**
- Each entry is one complete `(driver, branch, pay_item, effective_date)` version with every required slot.
- No individual slot-only saves in any form.
- All entries are validated before any are written.
- **Fail-all-or-none:** if any entry fails validation, the entire batch is rejected. No partial writes.
- On failure, the response identifies every failing entry with its specific error.

---

## Section 7 — Pay Item Creation Flow

### Schema Changes to `CustomPayItemCreate`

The request schema gains:

```python
class SlotDefinition(BaseModel):
    slot_name:     str             # non-blank, max 100 characters
    from_unit:     Decimal | None = None
    to_unit:       Decimal | None = None   # exclusive upper bound for range methods
    is_open_ended: bool = False

class CustomPayItemCreate(BaseModel):
    # ... existing fields ...
    slots: list[SlotDefinition] = []
    # block_size and rounding_rule for Block method
    block_size:    Decimal | None = None
    rounding_rule: str | None = None
```

For backward compatibility with the wizard's `rate_names` array, the service converts `rate_names` into `SlotDefinition` objects (`slot_name=name, from_unit=None, to_unit=None, is_open_ended=False`) before processing. Range methods must supply full slot definitions including boundaries.

### Changes to `create_custom_pay_item`

The function must:
1. Validate method-specific slot rules (see below).
2. INSERT `PayItems` with `IsSlotBased = TRUE`.
3. For Block: write `BlockSize` and `RoundingRule` on the `PayItems` row.
4. INSERT `PayItemRateSlots` rows — one per slot — in order of `SlotIndex`.
5. **Not** create any `RateTypes` rows.
6. **Not** create any `PayItemRateTypeMap` rows.
7. **Not** write `rate_name_N` keys to `PayItemSettings`.

All inserts occur in a single transaction.

**Deprecate and remove** `backfill_custom_pay_item_rate_structure` — it repairs the old CPI_ model that no longer applies.

### Method-Specific Slot Validation

**PerUnit:**
- Exactly 1 slot.
- `from_unit = None`, `to_unit = None`, `is_open_ended = False`.

**OrdinalTier:**
- 1 to `ORDINAL_TIER_MAX_SLOTS` slots (configurable constant, recommended default: 20).
- Slots are ordered by `SlotIndex` (1, 2, 3, ...).
- **All slots have `is_open_ended = False`** (including the final slot). The calculation engine automatically uses the highest-`SlotIndex` slot's rate for all ordinal positions at or above that index. No open-ended flag is stored or read.
- `from_unit = None`, `to_unit = None` for all slots.

**RangeBracket and RangeProgressive:**
- 2 to `RANGE_MAX_SLOTS` slots (configurable constant, recommended default: 20).
- All slots require `from_unit` and either `to_unit` or `is_open_ended = True`.
- Validation:
  1. First slot: `from_unit` must equal 0.
  2. Continuity: `slot[i+1].from_unit == slot[i].to_unit` (exact equality — no gaps, no overlaps).
  3. No intermediate slot may have `is_open_ended = True`.
  4. The final slot (highest `SlotIndex`) must have `is_open_ended = True` and `to_unit = None`.

**Block:**
- Exactly 1 slot.
- `from_unit = None`, `to_unit = None`, `is_open_ended = False`.
- `block_size` and `rounding_rule` must be present in the request.
- `rounding_rule` must be one of: `'Floor'`, `'Ceiling'`, `'NearestHalfUp'`.

### Does a Custom Item Need Any RateType?

**No.** The calculation engine dispatch routes on `PayItems.IsSlotBased`. The finalization query resolves rates via `PayItemID` directly. No `RateType` or `PayItemRateTypeMap` row is needed for any custom slot-based item.

---

## Section 8 — Calculation Engine

### Dispatch Rule

In `_compute_calculated_amount`, the routing is:

```python
if pay_item['is_slot_based']:
    return await _compute_slot_based(...)
else:
    # existing RateType-based path — adapted for evidence columns (see below)
    return await _compute_rate_type_based(...)
```

`is_slot_based` comes from `PayItems.IsSlotBased`.

### Shared Evidence Contract

**Both paths must return the same evidence tuple:**

```python
# Return type for all calculation helpers (slot-based and system)
CalcResult = tuple[
    Decimal | None,   # calculated amount (None = NMR)
    bool,             # needs_manager_review
    int | None,       # resolved_driver_rate_id (None = NMR)
    dict | None,      # calculation_breakdown (None = NMR)
]
```

`_refresh_draft_calculations` calls whichever path is appropriate, receives this tuple, and writes all four evidence fields atomically regardless of which path produced them.

### System/Default Rate Path — Return-Contract Adaptation

The existing functions (`_compute_per_unit`, `_compute_ordinal_tier`, `_compute_range_bracket`, `_compute_range_progressive`, `_compute_block`) currently return only `(amount, nmr)`. They must be adapted to also return `(driver_rate_id, breakdown)`.

**What changes:** the return contract and the `UPDATE` statement in `_refresh_draft_calculations`.

**What does not change:** all calculation formulas, rate-resolution queries, tier-loading queries (`_load_tiers`), rounding logic, and NMR conditions. The mathematical behavior of every existing function is preserved exactly.

#### System CalculationBreakdown Format

Each existing function produces a method-appropriate breakdown. The `driver_rate_id` field is the `DriverRateID` of the resolved `DriverRates` row.

**System PerUnit:**
```json
{
  "behavior": "PerUnit",
  "quantity": "450.0000",
  "rate_type_id": 15,
  "rate_type_code": "MILEAGE",
  "amount_per_unit": "0.8500",
  "total": "382.5000"
}
```

**System OrdinalTier (using DriverRateTiers):**
```json
{
  "behavior": "OrdinalTier",
  "quantity": "4",
  "rate_type_id": 22,
  "rate_type_code": "LOAD_TIERED",
  "tiers": [
    { "tier_sequence": 1, "from_unit": "0", "to_unit": "1.0000",
      "tier_amount": "35.0000", "units_applied": "1", "contribution": "35.0000" },
    { "tier_sequence": 2, "from_unit": "1.0000", "to_unit": "2.0000",
      "tier_amount": "30.0000", "units_applied": "1", "contribution": "30.0000" },
    { "tier_sequence": 3, "from_unit": "2.0000", "to_unit": null,
      "tier_amount": "25.0000", "units_applied": "2", "contribution": "50.0000" }
  ],
  "total": "115.0000"
}
```

**System Block:**
```json
{
  "behavior": "Block",
  "entered_quantity": "7.0000",
  "rate_type_id": 30,
  "rate_type_code": "PALLETS",
  "block_size": "5.0000",
  "rounding_rule": "Floor",
  "raw_block_count": "1.4000",
  "resolved_block_count": 1,
  "amount_per_block": "100.0000",
  "total": "100.0000"
}
```

For system RangeBracket and RangeProgressive, the breakdown follows the same structure as the slot-based equivalents in Section 11, replacing `slot_id`/`slot_index` with `tier_sequence` and adding `rate_type_id`/`rate_type_code`.

#### Stale Detection for System Rates

`approve_rate` and `void_rate` already know the `DriverRateID` being superseded or voided. The stale detection query (Section 10) uses `ResolvedDriverRateID` — which is the `DriverRates.DriverRateID` for both system and custom items. No separate detection path is needed for system rates. The single stale-marking UPDATE covers both.

### `_compute_slot_based` — Step-by-Step Resolution

1. **Load active structural slots:**
   ```sql
   SELECT PayItemRateSlotID, SlotIndex, SlotName, FromUnit, ToUnit, IsOpenEnded
   FROM payroll.PayItemRateSlots
   WHERE PayItemID = :pid AND Status = 'Active'
   ORDER BY SlotIndex ASC
   ```

2. **Resolve one branch-scoped DriverRates version effective on WorkDate:**
   ```sql
   SELECT dr.DriverRateID, dr.EffectiveFrom, dr.EffectiveTo, dr.Status
   FROM payroll.DriverRates dr
   WHERE dr.CompanyID     = :company_id
     AND dr.BranchID      = :branch_id
     AND dr.DriverID      = :driver_id
     AND dr.PayItemID     = :pay_item_id
     AND dr.Status        IN ('Approved', 'Superseded')
     AND dr.EffectiveFrom <= :work_date
     AND (dr.EffectiveTo IS NULL OR dr.EffectiveTo >= :work_date)
   ORDER BY dr.EffectiveFrom DESC
   LIMIT 1
   ```

3. **If no row found → return NMR (needs_manager_review = True, amount = None, rate_id = None, breakdown = None).**

4. **Load slot values:**
   ```sql
   SELECT drsv.PayItemRateSlotID, drsv.Amount
   FROM payroll.DriverRateSlotValues drsv
   WHERE drsv.DriverRateID = :rate_id
   ```

5. **Validate completeness:**
   - `count(slot_values) == count(active_slots)`.
   - Every required `PayItemRateSlotID` is present exactly once.
   - Any failure → NMR.

6. **Calculate by behavior** (see below).

7. **Produce `CalculationBreakdown`** (structured JSON — see Section 11).

8. **Return** `(amount, False, driver_rate_id, breakdown)`.

9. **Store atomically in one UPDATE** (performed by `_refresh_draft_calculations`):
   ```sql
   UPDATE payroll.PayrollDraftLines
   SET    CalculatedAmount     = :amount,
          ResolvedDriverRateID = :rate_id,
          CalculationBreakdown = :breakdown,
          CalculatedAtUtc      = NOW(),
          IsStale              = FALSE
   WHERE  DraftLineID = :line_id
   ```

### Calculation by Behavior (Custom Slot-Based)

**PerUnit:**
```python
result = quantity * slot_values[0].amount
```

**OrdinalTier:**
```python
# quantity must be a whole number (validated at entry; return NMR if decimal)
q = int(quantity)
slot_lookup = {s.slot_index: s for s in sorted_slots}
max_index = max(slot_lookup.keys())
result = Decimal(0)
for i in range(1, q + 1):
    idx = min(i, max_index)   # final slot catches all higher positions; IsOpenEnded is not read
    result += slot_values[idx].amount
```

**RangeBracket:**
```python
# Half-open [from, to): from_unit <= qty < to_unit
selected = None
for slot in sorted_slots:    # sorted by slot_index ASC
    if slot.is_open_ended:   # final range slot: from_unit <= qty
        selected = slot
        break
    if slot.from_unit <= quantity < slot.to_unit:
        selected = slot
        break
result = quantity * slot_values[selected.slot_id].amount
```

**RangeProgressive:**
```python
remaining = quantity
result = Decimal(0)
for slot in sorted_slots:    # sorted by slot_index ASC
    if remaining <= 0:
        break
    if slot.is_open_ended:
        units_in_slot = remaining
    else:
        capacity = slot.to_unit - slot.from_unit
        units_in_slot = min(remaining, capacity)
    result += units_in_slot * slot_values[slot.slot_id].amount
    remaining -= units_in_slot
```

**Block:**
```python
# block_size and rounding_rule come from PayItems, not DriverRates
raw = quantity / pay_item.block_size
if pay_item.rounding_rule == 'Floor':
    blocks = math.floor(raw)
elif pay_item.rounding_rule == 'Ceiling':
    blocks = math.ceil(raw)
else:  # NearestHalfUp
    blocks = math.floor(raw + Decimal('0.5'))
result = Decimal(blocks) * slot_values[0].amount
```

### Fail-Closed Behavior

| Condition | Result |
|---|---|
| No Approved/Superseded version for driver/branch/item/date | NMR |
| PendingApproval only | NMR |
| Missing slot value | NMR |
| Duplicate slot value | NMR (service prevents creation; engine defends) |
| Unknown slot ID (not in active PayItemRateSlots) | NMR |
| Retired slot | NMR |
| Range gap (detected at item creation; engine returns NMR if gap found at runtime) | NMR |
| Range overlap | NMR |
| No open-ended final slot on range item | NMR |
| Decimal quantity for OrdinalTier | NMR (HTTP 422 at draft entry validation is primary; engine also returns NMR) |
| NULL BlockSize on PayItem (should be prevented at creation) | NMR |
| NULL RoundingRule on PayItem | NMR |
| CompanyID mismatch | NMR (query filters by CompanyID; no row returned) |
| BranchID mismatch | NMR (query filters by BranchID; no row returned) |
| Voided rate | NMR (Voided is excluded from `IN ('Approved', 'Superseded')`) |

### Existing System Functions

The following existing functions remain for system/default items. Their **calculation formulas are unchanged**. They require only a return-contract adaptation to also return `(driver_rate_id, breakdown)`:

- `_compute_per_unit`
- `_compute_ordinal_tier`
- `_compute_range_bracket`
- `_compute_range_progressive`
- `_compute_block`
- `_load_tiers`
- `_resolve_rate_behavior`

The new function `_compute_slot_based` handles all custom `IsSlotBased = TRUE` items.

---

## Section 9 — Draft Calculation Evidence

### Columns Added to `payroll.PayrollDraftLines`

| Column | Type | Nullable | Default | Purpose |
|---|---|---|---|---|
| `ResolvedDriverRateID` | INTEGER FK → DriverRates | Yes | NULL | The specific rate version that produced CalculatedAmount |
| `CalculationBreakdown` | JSONB | Yes | NULL | Full per-slot or per-tier evidence — written for all rate-driven lines |
| `CalculatedAtUtc` | TIMESTAMPTZ | Yes | NULL | When the calculation was last run |
| `IsStale` | BOOLEAN | No | FALSE | Set TRUE when the resolved rate is superseded/voided while period is frozen |

**Migration:** Added in migration **0043** (the first new migration, alongside `PayItemRateSlots`). These columns are required by both the staleness mechanism and the calculation engine, and must exist before any service changes are deployed.

### Universal Evidence Requirement

**All rate-driven payroll lines write all four evidence fields**, regardless of whether the line uses a system RateType-based rate or a custom slot-based rate. This is the only model compatible with the freeze/finalization design, because:

- Finalization reads `ResolvedDriverRateID` and `CalculationBreakdown` from the stored draft line for both paths.
- The pre-finalization stale check queries `IsStale` for all lines.
- The stale detection in `approve_rate` and `void_rate` uses `ResolvedDriverRateID` regardless of whether the rate is system or custom.

System lines use a simpler breakdown structure (rate_type_id, rate_type_code, amount fields); custom lines use the per-slot structure (Section 11). Both satisfy the non-NULL requirement.

**Write behavior:**
All four columns are written in the same `UPDATE` statement as `CalculatedAmount` in `_refresh_draft_calculations`, immediately after either `_compute_slot_based` or the adapted system path returns its result. This atomicity guarantee means `ResolvedDriverRateID` and `CalculationBreakdown` always describe the calculation that produced the stored `CalculatedAmount`.

**Clear behavior:**
- When a draft line is voided: `ResolvedDriverRateID = NULL`, `CalculationBreakdown = NULL`, `CalculatedAtUtc = NULL`, `IsStale = FALSE`.
- When `_refresh_draft_calculations` reruns for a line: all five columns are overwritten atomically.
- `IsStale` is reset to `FALSE` when `_refresh_draft_calculations` successfully recalculates a line.

**Immutability after frozen states:**
These columns have no DB-level immutability on draft lines. Application-layer immutability is enforced by: (a) `_refresh_draft_calculations` raising an internal error if the period is in `_FROZEN_CALCULATION_STATUSES`, and (b) the stale mechanism replacing silent recalculation.

**API exposure:**
`IsStale` is exposed in the payroll period API response so the UI can display which lines need attention. `CalculationBreakdown` and `ResolvedDriverRateID` are included in finalization evidence accessible through admin/audit endpoints.

**Snapshot consistency guarantee:**
Finalization reads `dl.ResolvedDriverRateID` and `dl.CalculationBreakdown` from the same row in the same SELECT. Because they are written atomically, they are guaranteed to describe the same rate version. Finalization does not execute a separate rate resolution query for any item type.

---

## Section 10 — Review Freeze and Stale Handling

### Authoritative Frozen-State Constant

```python
# Used in _refresh_draft_calculations guard and stale detection queries
_FROZEN_CALCULATION_STATUSES = frozenset({'InReview', 'Approved'})
```

This is derived from the actual repository status flow (Section 1):
- `Open`: calculations run freely.
- `InReview`: frozen — rate changes mark stale; `_refresh_draft_calculations` blocked.
- `Approved`: frozen — rate changes mark stale; `_refresh_draft_calculations` blocked; draft entry write-blocked by `_WRITE_BLOCKED_STATUSES`.
- `Locked` / `Archived`: period is immutable; draft lines do not exist in these states.

### Open Status

`_refresh_draft_calculations` may be called freely. No restriction. Lines are updated atomically.

### Open → InReview Transition

`_refresh_draft_calculations` runs once in the transition service function. After it completes, the period status changes to InReview. The evidence written to each line is the reviewed state.

### Frozen Status Guard in `_refresh_draft_calculations`

```python
if period.status in _FROZEN_CALCULATION_STATUSES:
    raise InternalCalculationError(
        f"Attempted to refresh calculations for a period with status '{period.status}'. "
        "This operation is not permitted for frozen periods (InReview, Approved)."
    )
```

This is an internal guard — not an HTTP error — that causes the calling function to fail loudly rather than silently recalculate reviewed amounts.

### Rate Superseded or Voided During a Frozen Period

After `approve_rate` completes (Step 10 of the approval flow), and after `void_rate` completes, the service runs:

```sql
UPDATE payroll.PayrollDraftLines
SET    IsStale = TRUE
WHERE  ResolvedDriverRateID = :superseded_or_voided_rate_id
  AND  IsVoid = FALSE
  AND  PayrollPeriodID IN (
      SELECT PayrollPeriodID FROM payroll.PayrollPeriods
      WHERE  Status IN ('InReview', 'Approved')
         AND CompanyID = :company_id
  )
```

This covers:
- Lines in `InReview` periods (frozen, under manager review).
- Lines in `Approved` periods (frozen, awaiting finalization call).
- Both system RateType-based lines and custom slot-based lines (identified by `ResolvedDriverRateID`).

This runs inside the same transaction as the approval or void, after the advisory lock is held.

### Finalization Blocked While Stale

Pre-finalization validation (replaces the current `_refresh_draft_calculations` call):

```sql
SELECT COUNT(*)
FROM payroll.PayrollDraftLines
WHERE PayrollPeriodID = :period_id
  AND IsStale = TRUE
  AND IsVoid  = FALSE
```

If count > 0 → HTTP 422 "The payroll period cannot be finalized because X draft line(s) have stale calculations. Return the period to Open, recalculate, and re-enter review."

### Resolving Stale Lines

1. Return the period to Open (status transition).
2. `_refresh_draft_calculations` runs.
3. Stale lines are recalculated using the current approved rate. `IsStale = FALSE` is written atomically.
4. Transition to InReview again.
5. Manager reviews the new amounts.
6. Transition to Approved.
7. Finalization proceeds.

### Affected Functions

| Function | Change |
|---|---|
| `finalize_period` | Remove `_refresh_draft_calculations` call. Add 5-point pre-finalization validation. |
| `_refresh_draft_calculations` | Add `_FROZEN_CALCULATION_STATUSES` guard at the top. Write `ResolvedDriverRateID`, `CalculationBreakdown`, `CalculatedAtUtc`, `IsStale=FALSE` atomically with `CalculatedAmount` for all rate paths. |
| `approve_rate` | After advisory lock, after approval commit: run stale detection with `IN ('InReview', 'Approved')` filter. |
| `void_rate` | Same stale detection after voiding. |
| `_transition_to_in_review` | Confirm `_refresh_draft_calculations` is called here and that it is the last refresh before InReview. |

---

## Section 11 — CalculationBreakdown Structure

All numeric values are stored as decimal-safe strings in JSON.

### PerUnit (Custom Slot-Based)

```json
{
  "behavior": "PerUnit",
  "quantity": "8.5000",
  "slots": [
    {
      "slot_id": 7,
      "slot_index": 1,
      "slot_name": "Mileage Rate",
      "amount_per_unit": "0.8500",
      "quantity_applied": "8.5000",
      "contribution": "7.2250"
    }
  ],
  "total": "7.2250"
}
```

### OrdinalTier (Custom Slot-Based)

All slots have `"is_open_ended": false` in stored data. The breakdown records which ordinal positions each slot handled.

```json
{
  "behavior": "OrdinalTier",
  "quantity": "4",
  "slots": [
    {
      "slot_id": 1, "slot_index": 1, "slot_name": "First Load Rate",
      "is_open_ended": false,
      "amount_per_unit": "35.0000",
      "ordinal_positions": [1],
      "units_applied": "1",
      "contribution": "35.0000"
    },
    {
      "slot_id": 2, "slot_index": 2, "slot_name": "Second Load Rate",
      "is_open_ended": false,
      "amount_per_unit": "30.0000",
      "ordinal_positions": [2],
      "units_applied": "1",
      "contribution": "30.0000"
    },
    {
      "slot_id": 3, "slot_index": 3, "slot_name": "Third and Later Load Rate",
      "is_open_ended": false,
      "amount_per_unit": "25.0000",
      "ordinal_positions": [3, 4],
      "units_applied": "2",
      "contribution": "50.0000"
    }
  ],
  "total": "115.0000"
}
```

### RangeBracket (Custom Slot-Based)

```json
{
  "behavior": "RangeBracket",
  "quantity": "15.0000",
  "selected_slot": {
    "slot_id": 21, "slot_index": 2, "slot_name": "Mid Range",
    "from_unit": "10.0000",
    "to_unit_exclusive": "20.0000",
    "is_open_ended": false,
    "amount_per_unit": "2.0000"
  },
  "contribution": "30.0000",
  "total": "30.0000"
}
```

### RangeProgressive (Custom Slot-Based)

```json
{
  "behavior": "RangeProgressive",
  "quantity": "25.0000",
  "slots": [
    {
      "slot_id": 10, "slot_index": 1, "slot_name": "Tier 1",
      "from_unit": "0",
      "to_unit_exclusive": "10.0000",
      "is_open_ended": false,
      "amount_per_unit": "1.0000",
      "units_applied": "10.0000",
      "contribution": "10.0000"
    },
    {
      "slot_id": 11, "slot_index": 2, "slot_name": "Tier 2",
      "from_unit": "10.0000",
      "to_unit_exclusive": "20.0000",
      "is_open_ended": false,
      "amount_per_unit": "2.0000",
      "units_applied": "10.0000",
      "contribution": "20.0000"
    },
    {
      "slot_id": 12, "slot_index": 3, "slot_name": "Tier 3",
      "from_unit": "20.0000",
      "to_unit_exclusive": null,
      "is_open_ended": true,
      "amount_per_unit": "3.0000",
      "units_applied": "5.0000",
      "contribution": "15.0000"
    }
  ],
  "total": "45.0000"
}
```

### Block (Custom Slot-Based)

```json
{
  "behavior": "Block",
  "entered_quantity": "7.0000",
  "block_size": "5.0000",
  "rounding_rule": "Floor",
  "raw_block_count": "1.4000",
  "resolved_block_count": 1,
  "amount_per_block": "100.0000",
  "contribution": "100.0000",
  "total": "100.0000"
}
```

---

## Section 12 — Finalization and Immutable Evidence

### Column Assignments for Custom Slot-Based Items

| `PayrollFinalLines` column | Value for custom slot-based items |
|---|---|
| `RateTypeID` | **NULL** |
| `DriverRateID` | `PayrollDraftLines.ResolvedDriverRateID` |
| `ResolvedRateAmount` | **NULL** for multi-slot items; single slot amount for PerUnit (for consistency with existing PerUnit behavior) |
| `FinalAmount` | `dl.CalculatedAmount` — the stored reviewed amount |
| `SourceSnapshot` | Contains `calculation` key (from `dl.CalculationBreakdown`) plus stable metadata |

### SourceSnapshot for Custom Slot-Based Items

```sql
JSONB_STRIP_NULLS(JSONB_BUILD_OBJECT(
    'pay_item_id',          pi_sub.PayItemID,
    'pay_item_code',        pi_sub.PayItemCode,
    'pay_item_name',        pi_sub.PayItemName,
    'rate_behavior',        pi_sub.RateBehavior,
    'is_slot_based',        TRUE,
    'block_size',           pi_sub.BlockSize,
    'rounding_rule',        pi_sub.RoundingRule,
    'driver_rate_id',       dl.ResolvedDriverRateID,
    'calculation',          dl.CalculationBreakdown,
    'finalized_at',         NOW()
)) AS sourcesnapshot
```

The `calculation` key embeds the full `CalculationBreakdown` that was produced at review time. No separate metadata lookup by effective date is performed. All rate-related metadata (slot names, amounts, boundaries) is already inside `CalculationBreakdown`.

If additional metadata is needed (e.g., driver rate status or effective dates for audit):
```sql
SELECT Status, EffectiveFrom, EffectiveTo
FROM payroll.DriverRates
WHERE DriverRateID = :resolved_driver_rate_id
```
This is a direct lookup by the stored ID, not an effective-date re-resolution.

### Finalization Must Not Re-Resolve Rates

The finalization INSERT SELECT does not execute a `rate_sub` LATERAL for custom slot-based items. The `DriverRateID` value for custom items comes from `dl.ResolvedDriverRateID`. The existing `rate_sub` LATERAL (which resolves via `PayItemRateTypeMap`) continues to run only for `IsSlotBased = FALSE` items.

### Pre-Finalization Validation (5-Point)

Replaces the current `_refresh_draft_calculations` call in `finalize_period`:

1. No non-void draft lines have `IsStale = TRUE`.
2. All rate-requiring non-void lines have `CalculatedAmount IS NOT NULL`.
3. All rate-requiring non-void lines have `ResolvedDriverRateID IS NOT NULL`.
4. All referenced `ResolvedDriverRateID` values have status `Approved` or `Superseded` (not Voided).
5. All non-void lines with a resolved rate have `CalculationBreakdown IS NOT NULL`.

Check 5 applies to both system and custom lines. After Phase 5 deploys and the Open→InReview transition runs `_refresh_draft_calculations` (which now writes CalculationBreakdown for all paths), this check will pass for all rate-requiring lines.

### Queries That Must Be Reviewed Before Deployment

Before Phase 7 (Finalization) is deployed, every query in the following areas must be audited for assumptions that `RateTypeID IS NOT NULL` or `ResolvedRateAmount IS NOT NULL`:

- All queries in `payroll/service.py` that JOIN `PayrollFinalLines` to `RateTypes` on `RateTypeID`
- Any report queries in `app/reports/` or equivalent that GROUP BY or FILTER on `RateTypeID`
- Any ledger queries that use `ResolvedRateAmount` without a NULL guard
- Any export or preview function that reads `RateTypeID` or `ResolvedRateAmount` to display a rate label

**Rule:** Report and ledger totals must use `FinalAmount`. Display of rate labels for custom items must read from `SourceSnapshot->>'calculation'` or from `DriverRateSlotValues` joined by `DriverRateID`.

---

## Section 13 — Structural Immutability

### When Structure Becomes Immutable

A custom Pay Item's structure becomes immutable once **any** of the following is true:

- Any `DriverRates` row exists with `PayItemID = :pid AND Status != 'Voided'` (includes PendingApproval).
- Any `PayrollDraftLines` row has `PayItemID = :pid`.
- Any `PayrollFinalLines` row has `PayItemID = :pid`.

**Including PendingApproval is required** because `DriverRateSlotValues` child rows are already written against the current slot definitions. Adding, removing, or reordering slots after a pending version exists would leave the slot-value rows inconsistent.

### Protected Fields

| Change | Blocked after immutability? |
|---|---|
| Adding a new slot (INSERT into PayItemRateSlots) | Yes |
| Deleting a slot (DELETE from PayItemRateSlots) | Yes |
| Retiring a slot (Status → 'Retired') | Yes |
| Changing SlotIndex | Yes |
| Changing FromUnit | Yes |
| Changing ToUnit | Yes |
| Changing IsOpenEnded | Yes |
| Changing RateBehavior on PayItems | Yes |
| Changing IsSlotBased on PayItems | Yes |
| Changing BlockSize on PayItems | Yes |
| Changing RoundingRule on PayItems | Yes |
| **Renaming SlotName** | **No — allowed** |

Renaming `SlotName` is safe because the `CalculationBreakdown` stored at review time captures the slot name as it was at that moment. Finalized snapshots are unaffected by later renames.

### Important: Trigger Dependency on `DriverRates.PayItemID`

The structural triggers below query `payroll.DriverRates.PayItemID`. That column does not exist until migration 0044. Therefore **both triggers are installed in migration 0044**, not 0043. Migration 0043 creates the `PayItemRateSlots` table and `DriverRateSlotValues` table but installs only the `DriverRateSlotValues` mutation guard (which does not query `DriverRates.PayItemID`).

### Trigger Design — `fn_guard_payitemrateslot_structural_mutation`

Installed in migration 0044, after `DriverRates.PayItemID` has been added.

```sql
CREATE OR REPLACE FUNCTION payroll.fn_guard_payitemrateslot_structural_mutation()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    v_pay_item_id INTEGER;
    v_in_use      BOOLEAN;
BEGIN
    -- Safely obtain PayItemID without referencing NEW on DELETE
    CASE TG_OP
        WHEN 'INSERT' THEN v_pay_item_id := NEW.PayItemID;
        WHEN 'UPDATE' THEN v_pay_item_id := OLD.PayItemID;
        WHEN 'DELETE' THEN v_pay_item_id := OLD.PayItemID;
    END CASE;

    SELECT EXISTS (
        SELECT 1 FROM payroll.DriverRates
        WHERE  PayItemID = v_pay_item_id AND Status != 'Voided'
        UNION ALL
        SELECT 1 FROM payroll.PayrollDraftLines
        WHERE  PayItemID = v_pay_item_id
        UNION ALL
        SELECT 1 FROM payroll.PayrollFinalLines
        WHERE  PayItemID = v_pay_item_id
    ) INTO v_in_use;

    -- Item not yet in use: allow all operations
    IF NOT v_in_use THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;   -- allow delete; returning OLD (not NEW) is required for BEFORE DELETE
        ELSE
            RETURN NEW;   -- allow insert or update
        END IF;
    END IF;

    -- Item IS in use. Enforce structural protection.

    IF TG_OP = 'INSERT' THEN
        RAISE EXCEPTION
            'Cannot add a slot to pay item % because it has active rate versions or payroll usage.',
            v_pay_item_id USING ERRCODE = 'P0001';
    END IF;

    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION
            'Cannot delete slot % from pay item % because it has active rate versions or payroll usage.',
            OLD.PayItemRateSlotID, v_pay_item_id USING ERRCODE = 'P0001';
    END IF;

    -- UPDATE: block structural fields; allow SlotName rename
    IF OLD.SlotIndex   IS DISTINCT FROM NEW.SlotIndex
    OR OLD.FromUnit    IS DISTINCT FROM NEW.FromUnit
    OR OLD.ToUnit      IS DISTINCT FROM NEW.ToUnit
    OR OLD.IsOpenEnded IS DISTINCT FROM NEW.IsOpenEnded
    THEN
        RAISE EXCEPTION
            'Cannot change structural fields on slot % of pay item % because it has active rate versions or payroll usage.',
            OLD.PayItemRateSlotID, v_pay_item_id USING ERRCODE = 'P0001';
    END IF;

    IF NEW.Status = 'Retired' AND OLD.Status = 'Active' THEN
        RAISE EXCEPTION
            'Cannot retire slot % of pay item % because it has active rate versions or payroll usage.',
            OLD.PayItemRateSlotID, v_pay_item_id USING ERRCODE = 'P0001';
    END IF;

    -- SlotName change only — permit
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_guard_payitemrateslot_structural_mutation
    BEFORE INSERT OR UPDATE OR DELETE
    ON payroll.PayItemRateSlots
    FOR EACH ROW
    EXECUTE FUNCTION payroll.fn_guard_payitemrateslot_structural_mutation();
```

### Trigger Design — `fn_guard_payitem_structural_mutation`

Installed in migration 0044, after `DriverRates.PayItemID` has been added.

```sql
CREATE OR REPLACE FUNCTION payroll.fn_guard_payitem_structural_mutation()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    v_in_use BOOLEAN;
BEGIN
    -- Only fires on UPDATE (WHEN clause on trigger restricts to IsSlotBased = TRUE)
    SELECT EXISTS (
        SELECT 1 FROM payroll.DriverRates
        WHERE  PayItemID = OLD.PayItemID AND Status != 'Voided'
        UNION ALL
        SELECT 1 FROM payroll.PayrollDraftLines
        WHERE  PayItemID = OLD.PayItemID
        UNION ALL
        SELECT 1 FROM payroll.PayrollFinalLines
        WHERE  PayItemID = OLD.PayItemID
    ) INTO v_in_use;

    IF NOT v_in_use THEN
        RETURN NEW;
    END IF;

    IF OLD.RateBehavior  IS DISTINCT FROM NEW.RateBehavior
    OR OLD.IsSlotBased   IS DISTINCT FROM NEW.IsSlotBased
    OR OLD.BlockSize     IS DISTINCT FROM NEW.BlockSize
    OR OLD.RoundingRule  IS DISTINCT FROM NEW.RoundingRule
    THEN
        RAISE EXCEPTION
            'Cannot change structural configuration of pay item % because it has active rate versions or payroll usage.',
            OLD.PayItemID USING ERRCODE = 'P0001';
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_guard_payitem_structural_mutation
    BEFORE UPDATE
    ON payroll.PayItems
    FOR EACH ROW
    WHEN (OLD.IsSlotBased = TRUE)
    EXECUTE FUNCTION payroll.fn_guard_payitem_structural_mutation();
```

### Service-Layer Pre-Check

Before any structural write, call:

```python
async def _assert_pay_item_structure_mutable(pay_item_id: int, db: AsyncConnection) -> None:
    row = await db.execute(text("""
        SELECT
            EXISTS(SELECT 1 FROM payroll.DriverRates
                   WHERE PayItemID = :pid AND Status != 'Voided') AS has_rates,
            EXISTS(SELECT 1 FROM payroll.PayrollDraftLines
                   WHERE PayItemID = :pid) AS has_draft,
            EXISTS(SELECT 1 FROM payroll.PayrollFinalLines
                   WHERE PayItemID = :pid) AS has_final
    """), {"pid": pay_item_id})
    r = row.one()
    if r.has_rates:
        raise HTTPException(422, "This pay item's structure cannot be changed because it has active rate versions.")
    if r.has_draft:
        raise HTTPException(422, "This pay item's structure cannot be changed because it has open payroll draft lines.")
    if r.has_final:
        raise HTTPException(422, "This pay item's structure cannot be changed because it has finalized payroll history.")
```

---

## Section 14 — Zero-Data Audit and Cleanup

### Pre-Migration Audit SQL

All eight queries must return zero before any cleanup proceeds.

```sql
-- 1. Company-owned non-system custom Pay Items
SELECT COUNT(*) AS custom_pay_items
FROM payroll.PayItems
WHERE CompanyID IS NOT NULL AND IsSystemStandard = FALSE;

-- 2. CPI_ RateTypes
SELECT COUNT(*) AS cpi_rate_types
FROM payroll.RateTypes
WHERE RateCode LIKE 'CPI\_%' ESCAPE '\';

-- 3. Custom PayItemRateTypeMap rows
SELECT COUNT(*) AS custom_map_rows
FROM payroll.PayItemRateTypeMap pirtm
JOIN payroll.PayItems pi ON pi.PayItemID = pirtm.PayItemID
WHERE pi.CompanyID IS NOT NULL AND pi.IsSystemStandard = FALSE;

-- 4. DriverRates linked to CPI_ RateTypes
SELECT COUNT(*) AS custom_driver_rates
FROM payroll.DriverRates dr
JOIN payroll.RateTypes rt ON rt.RateTypeID = dr.RateTypeID
WHERE rt.RateCode LIKE 'CPI\_%' ESCAPE '\';

-- 5. DriverRateTiers linked to CPI_-based DriverRates
SELECT COUNT(*) AS custom_rate_tiers
FROM payroll.DriverRateTiers drt
JOIN payroll.DriverRates dr ON dr.DriverRateID = drt.DriverRateID
JOIN payroll.RateTypes rt ON rt.RateTypeID = dr.RateTypeID
WHERE rt.RateCode LIKE 'CPI\_%' ESCAPE '\';

-- 6. Draft lines referencing custom Pay Items
SELECT COUNT(*) AS custom_draft_lines
FROM payroll.PayrollDraftLines pdl
JOIN payroll.PayItems pi ON pi.PayItemID = pdl.PayItemID
WHERE pi.CompanyID IS NOT NULL AND pi.IsSystemStandard = FALSE;

-- 7. Final lines referencing custom Pay Items
SELECT COUNT(*) AS custom_final_lines
FROM payroll.PayrollFinalLines pfl
JOIN payroll.PayItems pi ON pi.PayItemID = pfl.PayItemID
WHERE pi.CompanyID IS NOT NULL AND pi.IsSystemStandard = FALSE;

-- 8. Obsolete rate_name_% PayItemSettings for custom items
SELECT COUNT(*) AS obsolete_settings
FROM payroll.PayItemSettings pis
JOIN payroll.PayItems pi ON pi.PayItemID = pis.PayItemID
WHERE pi.CompanyID IS NOT NULL
  AND pi.IsSystemStandard = FALSE
  AND pis.SettingKey LIKE 'rate\_name\_%' ESCAPE '\';
```

### If Any Count Is Nonzero

There is no automatic conversion path. The correct resolution:

1. Identify the specific rows using the audit queries with `SELECT *` instead of `COUNT(*)`.
2. Verify they are synthetic/test items with no real business meaning.
3. Delete them in dependency order:

```sql
BEGIN;

-- a. Delete rate_name_N settings
DELETE FROM payroll.PayItemSettings
WHERE SettingKey LIKE 'rate\_name\_%' ESCAPE '\'
  AND PayItemID IN (
      SELECT PayItemID FROM payroll.PayItems
      WHERE CompanyID IS NOT NULL AND IsSystemStandard = FALSE
  );

-- b. Delete PayItemRateTypeMap rows for custom items
DELETE FROM payroll.PayItemRateTypeMap
WHERE PayItemID IN (
    SELECT PayItemID FROM payroll.PayItems
    WHERE CompanyID IS NOT NULL AND IsSystemStandard = FALSE
);

-- c. Delete DriverRateTiers for custom DriverRates
DELETE FROM payroll.DriverRateTiers
WHERE DriverRateID IN (
    SELECT dr.DriverRateID FROM payroll.DriverRates dr
    JOIN payroll.RateTypes rt ON rt.RateTypeID = dr.RateTypeID
    WHERE rt.RateCode LIKE 'CPI\_%' ESCAPE '\'
);

-- d. Delete custom DriverRates rows
DELETE FROM payroll.DriverRates
WHERE RateTypeID IN (
    SELECT RateTypeID FROM payroll.RateTypes
    WHERE RateCode LIKE 'CPI\_%' ESCAPE '\'
);

-- e. Delete CPI_ RateTypes
DELETE FROM payroll.RateTypes
WHERE RateCode LIKE 'CPI\_%' ESCAPE '\' AND CompanyID IS NOT NULL;

-- f. Delete the custom Pay Items themselves
DELETE FROM payroll.PayItems
WHERE CompanyID IS NOT NULL AND IsSystemStandard = FALSE;

COMMIT;
```

After deletion, re-run all 8 audit queries and confirm all return zero. Then run migration 0045.

Any synthetic or test custom Pay Items that were deleted must be recreated after Phase 3 deploys, using the new slot-based creation path. No automatic legacy conversion is introduced.

### Cleanup Migration (0045) — Scoped and Fail-Closed

Migration 0045 enforces its own fail-closed guards and must not be run unless all 8 audit queries have been confirmed zero on the target database.

```sql
BEGIN;

-- ============================================================
-- Guard 0: direct PayItem count (fail-closed primary guard)
-- This guard is the authoritative safety check. It ensures
-- the migration cannot partially dismantle a legacy custom item
-- by removing its RateTypes and settings without the item
-- having PayItemRateSlots to replace them.
-- ============================================================
DO $$
DECLARE v_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO v_count
    FROM payroll.PayItems
    WHERE CompanyID IS NOT NULL
      AND IsSystemStandard = FALSE;

    IF v_count > 0 THEN
        RAISE EXCEPTION
            'ABORT 0045: % legacy custom PayItems exist. '
            'Resolve synthetic/test items before cleanup; '
            'no automatic conversion is supported.',
            v_count;
    END IF;
END $$;

-- Guard 1: no CPI_ DriverRates
DO $$
DECLARE v INTEGER;
BEGIN
    SELECT COUNT(*) INTO v FROM payroll.DriverRates dr
    JOIN payroll.RateTypes rt ON rt.RateTypeID = dr.RateTypeID
    WHERE rt.RateCode LIKE 'CPI\_%' ESCAPE '\';
    IF v > 0 THEN
        RAISE EXCEPTION 'ABORT 0045: % DriverRates linked to CPI_ types. Resolve manually.', v;
    END IF;
END $$;

-- Guard 2: no custom draft lines
DO $$
DECLARE v INTEGER;
BEGIN
    SELECT COUNT(*) INTO v FROM payroll.PayrollDraftLines pdl
    JOIN payroll.PayItems pi ON pi.PayItemID = pdl.PayItemID
    WHERE pi.CompanyID IS NOT NULL AND pi.IsSystemStandard = FALSE;
    IF v > 0 THEN
        RAISE EXCEPTION 'ABORT 0045: % draft lines reference custom items.', v;
    END IF;
END $$;

-- Guard 3: no custom final lines
DO $$
DECLARE v INTEGER;
BEGIN
    SELECT COUNT(*) INTO v FROM payroll.PayrollFinalLines pfl
    JOIN payroll.PayItems pi ON pi.PayItemID = pfl.PayItemID
    WHERE pi.CompanyID IS NOT NULL AND pi.IsSystemStandard = FALSE;
    IF v > 0 THEN
        RAISE EXCEPTION 'ABORT 0045: % final lines reference custom items.', v;
    END IF;
END $$;

-- Guard 4: no unexpected non-rate PayItemSettings for custom items
DO $$
DECLARE v INTEGER;
BEGIN
    SELECT COUNT(*) INTO v FROM payroll.PayItemSettings pis
    JOIN payroll.PayItems pi ON pi.PayItemID = pis.PayItemID
    WHERE pi.CompanyID IS NOT NULL AND pi.IsSystemStandard = FALSE
      AND pis.SettingKey NOT LIKE 'rate\_name\_%' ESCAPE '\';
    IF v > 0 THEN
        RAISE EXCEPTION 'ABORT 0045: % non-rate PayItemSettings found for custom items. Manual review required.', v;
    END IF;
END $$;

-- All guards passed. Proceed with cleanup.

-- Delete only rate_name_N settings
DELETE FROM payroll.PayItemSettings
WHERE SettingKey LIKE 'rate\_name\_%' ESCAPE '\'
  AND PayItemID IN (
      SELECT PayItemID FROM payroll.PayItems
      WHERE CompanyID IS NOT NULL AND IsSystemStandard = FALSE
  );

-- Remove PayItemRateTypeMap for custom items only
DELETE FROM payroll.PayItemRateTypeMap
WHERE PayItemID IN (
    SELECT PayItemID FROM payroll.PayItems
    WHERE CompanyID IS NOT NULL AND IsSystemStandard = FALSE
);

-- Deactivate CPI_ RateTypes (preserve audit trail; do not delete)
UPDATE payroll.RateTypes
SET IsActive = FALSE
WHERE RateCode LIKE 'CPI\_%' ESCAPE '\' AND CompanyID IS NOT NULL;

-- Post-cleanup verification
DO $$
DECLARE v_s INTEGER; v_m INTEGER; v_t INTEGER;
BEGIN
    SELECT COUNT(*) INTO v_s FROM payroll.PayItemSettings pis
    JOIN payroll.PayItems pi ON pi.PayItemID = pis.PayItemID
    WHERE pi.CompanyID IS NOT NULL AND pis.SettingKey LIKE 'rate\_name\_%' ESCAPE '\';

    SELECT COUNT(*) INTO v_m FROM payroll.PayItemRateTypeMap pirtm
    JOIN payroll.PayItems pi ON pi.PayItemID = pirtm.PayItemID
    WHERE pi.CompanyID IS NOT NULL;

    SELECT COUNT(*) INTO v_t FROM payroll.RateTypes
    WHERE RateCode LIKE 'CPI\_%' ESCAPE '\' AND IsActive = TRUE;

    IF v_s > 0 OR v_m > 0 OR v_t > 0 THEN
        RAISE EXCEPTION 'ABORT 0045: post-cleanup check failed. settings=%, maps=%, active_cpi=%.', v_s, v_m, v_t;
    END IF;
END $$;

COMMIT;
```

---

## Section 15 — System/Default Item Isolation

### Dispatch Rule

```python
def _is_slot_based(pay_item: dict) -> bool:
    return pay_item.get('is_slot_based') is True
```

`IsSlotBased` is the single authoritative dispatch flag. It is set to `TRUE` only for new custom items created after this architecture is deployed. It is permanently `FALSE` for all system items.

**Do not rely on `CompanyID IS NULL` as the dispatch.** A company could theoretically have a future custom item that uses the legacy RateType path (e.g., migrated or admin-created). The `IsSlotBased` flag is explicit and unambiguous.

**Do not rely on the `CPI_` prefix in `RateCode`.** The prefix is a naming convention, not a semantic guarantee.

### System Items — Unchanged Behavior

| Aspect | System items | Custom slot-based items |
|---|---|---|
| `RateTypes` | Used | Not used |
| `PayItemRateTypeMap` | Used | Not used |
| `DriverRates.RateTypeID` | Set | NULL |
| `DriverRates.PayItemID` | NULL | Set |
| `DriverRates.Amount` | Set and used in calculation | NULL |
| `DriverRateTiers` | Used for tiered system behaviors | Not used |
| `PayItemRateSlots` | Not used | Used |
| `DriverRateSlotValues` | Not used | Used |
| Calculation functions | `_compute_per_unit`, `_compute_ordinal_tier`, etc. (formula unchanged; return contract adapted) | `_compute_slot_based` |
| Evidence columns written | Yes — all four fields written atomically | Yes — all four fields written atomically |
| Finalization LATERAL | Existing `rate_sub` (unchanged) | New `rate_sub_slot` (direct DriverRateID lookup) |
| `PayrollFinalLines.RateTypeID` | Set | NULL |
| `PayrollFinalLines.ResolvedRateAmount` | Set for PerUnit | NULL (multi-slot) or single amount (PerUnit) |
| Stale detection | Yes — ResolvedDriverRateID used | Yes — ResolvedDriverRateID used |

The existing `_compute_*` functions, `_resolve_rate_behavior`, `_load_tiers`, and the existing finalization `rate_sub` and `tier_sub` LATERALs are **not removed or replaced**. They require only a return-contract adaptation to include `(driver_rate_id, breakdown)` alongside the existing `(amount, nmr)` return values.

---

## Section 16 — Migration Sequence

Starting from Alembic head **0042**.

---

### Migration 0043 — Additive: new tables and draft evidence columns

**Purpose:** Create `PayItemRateSlots`, create `DriverRateSlotValues`, add draft-line evidence columns, and install the `DriverRateSlotValues` mutation guard. The structural immutability triggers for `PayItemRateSlots` and `PayItems` are **not installed here** because they depend on `DriverRates.PayItemID`, which does not exist until migration 0044.

**Schema changes:**

```sql
-- 1. Create PayItemRateSlots
CREATE TABLE payroll.PayItemRateSlots ( ... );  -- full DDL in Section 3.1
-- uq_PayItemRateSlots_Item_Index
-- ck_PayItemRateSlots_OpenEnded   (CHECK (NOT IsOpenEnded OR ToUnit IS NULL))
-- ck_PayItemRateSlots_Bounds
-- ix_PayItemRateSlots_PayItemID

-- 2. Create DriverRateSlotValues
CREATE TABLE payroll.DriverRateSlotValues ( ... );  -- full DDL in Section 3.2
-- ON DELETE CASCADE on DriverRateID
-- ON DELETE RESTRICT on PayItemRateSlotID
-- uq_DriverRateSlotValues_Rate_Slot
-- ix_DriverRateSlotValues_DriverRateID

-- 3. Add draft-line evidence columns
ALTER TABLE payroll.PayrollDraftLines
    ADD COLUMN ResolvedDriverRateID INTEGER NULL REFERENCES payroll.DriverRates(DriverRateID),
    ADD COLUMN CalculationBreakdown JSONB NULL,
    ADD COLUMN CalculatedAtUtc TIMESTAMPTZ NULL,
    ADD COLUMN IsStale BOOLEAN NOT NULL DEFAULT FALSE;
-- ix_PayrollDraftLines_ResolvedDriverRateID
-- ix_PayrollDraftLines_Stale

-- 4. Install immutability trigger on DriverRateSlotValues
-- (does NOT query DriverRates.PayItemID — safe to install now)
-- fn_guard_driverrateslotvalue_used_mutation
-- trg_guard_driverrateslotvalue_used_mutation
```

**Triggers NOT installed in this migration** (depend on `DriverRates.PayItemID`):
- `fn_guard_payitemrateslot_structural_mutation`
- `trg_guard_payitemrateslot_structural_mutation`
- `fn_guard_payitem_structural_mutation`
- `trg_guard_payitem_structural_mutation`

These are installed in migration 0044 after `DriverRates.PayItemID` is added.

**Verification queries:**
```sql
SELECT COUNT(*) FROM payroll.PayItemRateSlots;      -- must = 0
SELECT COUNT(*) FROM payroll.DriverRateSlotValues;  -- must = 0
SELECT column_name FROM information_schema.columns
WHERE table_name = 'payrolldraftlines' AND column_name = 'isstale';  -- must return row
```

**Downgrade:** DROP TABLE `DriverRateSlotValues`, DROP TABLE `PayItemRateSlots`, DROP COLUMNs from `PayrollDraftLines`, DROP triggers and functions. Safe while new tables are empty.

**Risk:** Low — purely additive. No existing rows affected. New triggers fire on new tables only.

---

### Migration 0044 — Alter `PayItems` and `DriverRates`; install structural triggers

**Purpose:** Add `IsSlotBased`, `BlockSize`, `RoundingRule` to `PayItems`; add `PayItemID` to `DriverRates`; make `RateTypeID` and `Amount` nullable; install check constraints, indexes, GIST exclusion; and install all structural immutability triggers (which now have their dependency on `DriverRates.PayItemID` satisfied).

**Schema changes (in dependency order):**

```sql
-- PayItems additions
ALTER TABLE payroll.PayItems
    ADD COLUMN IsSlotBased  BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN BlockSize    NUMERIC(18,4) NULL,
    ADD COLUMN RoundingRule VARCHAR(20) NULL;

-- Block config constraint — scoped to IsSlotBased = TRUE rows only
ALTER TABLE payroll.PayItems
    ADD CONSTRAINT ck_PayItems_BlockConfig CHECK (
        IsSlotBased = FALSE
        OR (
            (RateBehavior = 'Block'
             AND BlockSize IS NOT NULL AND BlockSize > 0
             AND RoundingRule IS NOT NULL
             AND RoundingRule IN ('Floor', 'Ceiling', 'NearestHalfUp'))
            OR
            (RateBehavior != 'Block' AND BlockSize IS NULL AND RoundingRule IS NULL)
        )
    );

-- DriverRates step 1: add PayItemID
ALTER TABLE payroll.DriverRates
    ADD COLUMN PayItemID INTEGER NULL REFERENCES payroll.PayItems(PayItemID);

-- DriverRates step 2: drop NOT NULL from RateTypeID (MUST precede XOR constraint)
ALTER TABLE payroll.DriverRates
    ALTER COLUMN RateTypeID DROP NOT NULL;

-- DriverRates step 3: make Amount nullable
ALTER TABLE payroll.DriverRates
    ALTER COLUMN Amount DROP NOT NULL;

-- DriverRates step 4: check constraints
ALTER TABLE payroll.DriverRates
    ADD CONSTRAINT ck_DriverRates_ItemOrType CHECK ( ... );         -- Section 3.3
ALTER TABLE payroll.DriverRates
    ADD CONSTRAINT ck_DriverRates_Amount_Presence CHECK ( ... );
ALTER TABLE payroll.DriverRates
    ADD CONSTRAINT ck_DriverRates_Block_Slot_Exclusion CHECK ( ... );

-- DriverRates step 5: indexes
CREATE UNIQUE INDEX ux_DriverRates_Driver_PayItem_Approved ...;     -- Section 4
CREATE UNIQUE INDEX ux_DriverRates_Driver_PayItem_Pending ...;
CREATE INDEX ix_DriverRates_Driver_PayItem_Date ...;
ALTER TABLE payroll.DriverRates ADD CONSTRAINT excl_DriverRates_PayItem_no_overlap ...;

-- Step 6: NOW install structural triggers (dependency on PayItemID now satisfied)
-- fn_guard_payitemrateslot_structural_mutation (Section 13)
-- trg_guard_payitemrateslot_structural_mutation
-- fn_guard_payitem_structural_mutation (Section 13)
-- trg_guard_payitem_structural_mutation

-- Step 7: Update fn_guard_driverrate_used_mutation to include PayItemID in protected columns
```

**Verification queries:**
```sql
-- All existing rows satisfy the XOR constraint
SELECT COUNT(*) FROM payroll.DriverRates WHERE RateTypeID IS NULL;     -- must = 0
SELECT COUNT(*) FROM payroll.DriverRates WHERE PayItemID IS NOT NULL;  -- must = 0
SELECT COUNT(*) FROM payroll.DriverRates WHERE Amount IS NULL;         -- must = 0
```

**Downgrade preconditions:** No `PayItemID IS NOT NULL` rows may exist. `ALTER COLUMN RateTypeID SET NOT NULL` and `ALTER COLUMN Amount SET NOT NULL` must succeed (they will, given the above). Drop structural triggers first (they depend on `DriverRates.PayItemID`), then drop constraints and indexes in reverse order, then drop the `PayItemID` column.

**Downgrade order:**
1. DROP `trg_guard_payitem_structural_mutation`, `fn_guard_payitem_structural_mutation`
2. DROP `trg_guard_payitemrateslot_structural_mutation`, `fn_guard_payitemrateslot_structural_mutation`
3. DROP GIST exclusion, unique indexes, check constraints on `DriverRates`
4. `ALTER COLUMN Amount SET NOT NULL`, `ALTER COLUMN RateTypeID SET NOT NULL`
5. DROP COLUMN `DriverRates.PayItemID`
6. DROP constraint `ck_PayItems_BlockConfig`
7. DROP COLUMN `PayItems.RoundingRule`, `PayItems.BlockSize`, `PayItems.IsSlotBased`

**Risk:** Medium — alters two active production tables. Run inside a transaction. Test on a staging copy first. The `DROP NOT NULL` operations are safe for tables with all-populated existing columns.

---

### Migration 0045 — Scoped cleanup of legacy CPI_ artifacts

**Purpose:** Remove `rate_name_N` PayItemSettings, remove `PayItemRateTypeMap` rows, and deactivate `CPI_` RateTypes for company-owned custom items.

**Entry condition:** All 8 audit queries (Section 14) return zero on the target database, confirmed immediately before deployment.

**Schema changes:** None. Data cleanup only.

Full SQL is in Section 14 (cleanup migration body with Guard 0 + 4 additional guards + 3 cleanup statements + post-verification). Guard 0 (the direct custom PayItem count check) is the first statement and aborts the migration if any legacy custom PayItems exist.

**Downgrade:** Rows are deleted. Rollback within the transaction restores them. After commit, recovery requires re-running test/fixture setup. No real business data is lost (all 8 audit queries confirmed zero before running).

**Risk:** Low — zero real data exists. Fail-closed guards (including the direct PayItem count) prevent accidental deletion of unrelated data or partial dismantling of any custom item.

---

### Future Migration — `DriverRateTiers` Deprecation (Unnumbered)

`DriverRateTiers` is **not dropped in this plan**. It continues to serve system/default tiered items. A future migration may deprecate it after confirming no system item uses tiered rate types. That investigation is out of scope here. The migration number will be assigned after the current head advances past 0045.

---

## Section 17 — Final Implementation Phases

### Phase 0 — Audit

**Objective:** Confirm zero real custom-item data before any schema changes.

**Files/services:** None changed.

**Entry condition:** None.

**Work:** Run all 8 audit SQL queries against the production database. Record results including database name, timestamp, and the count returned by each query.

**Validation:** All 8 counts = 0.

**Risk:** None.

**Rollback:** N/A.

**Completion criteria:** Written record that all 8 counts are zero on the production database at a specific timestamp.

---

### Phase 1 — Additive Schema

**Objective:** Apply migrations 0043 and 0044. No behavior changes.

**Files/services:** Alembic migration files only.

**Entry condition:** Phase 0 complete.

**Work:**
1. Write and apply migration 0043 (new tables + draft evidence columns + `DriverRateSlotValues` mutation guard only).
2. Write and apply migration 0044 (PayItems + DriverRates alterations + all structural triggers).
3. Existing tests must pass without any service changes.

**Tests:** All existing tests must pass unchanged after these migrations.

**Validation commands:**
```bash
alembic upgrade head
pytest backend/tests/ -x
```

**Risk:** Medium for migration 0044 (alters active tables). Low for 0043 (new tables only).

**Rollback:** `alembic downgrade 0042`.

**Completion criteria:** Both migrations applied cleanly. All existing tests green.

---

### Phase 2 — Scoped Cleanup

**Objective:** Apply migration 0045 (cleanup). **Deploy simultaneously with Phase 3** (new creation path) in one release to prevent a window where the old code creates new CPI_ artifacts into a cleaned schema.

**Files/services:** Alembic migration 0045 only.

**Entry condition:** Phase 1 complete. All 8 audit queries re-confirmed zero on production immediately before deployment.

**Work:** Apply migration 0045 (fail-closed cleanup including Guard 0 direct PayItem check).

**Risk:** Low. Fail-closed guards protect against unexpected data. Guard 0 aborts the migration if any legacy custom PayItems exist.

**Rollback:** Rollback is only possible before commit. After commit, recovery requires re-running test fixture setup.

**Completion criteria:** Migration applied. Post-cleanup verification inside the migration passes.

**Note on timing:** Phases 2 and 3 must deploy in the same release window. If Phase 3 (new creation code) cannot be deployed immediately after Phase 2, introduce a temporary feature flag using an existing configuration mechanism (without a separate numbered migration) that causes the old custom-item creation endpoint to return 503. Once Phase 3 code is live, remove the flag.

---

### Phase 3 — New Custom Pay Item Creation Path

**Objective:** Update `create_custom_pay_item` to write `PayItemRateSlots` instead of RateTypes.

**Files/services:**
- `app/settings/service.py` — `create_custom_pay_item`
- `app/settings/schemas.py` — `CustomPayItemCreate`, `CustomPayItemRequestCreate`
- Remove `backfill_custom_pay_item_rate_structure` function

**Entry condition:** Phase 2 complete.

**Work:**
1. Update `CustomPayItemCreate` schema to accept `slots: list[SlotDefinition]` and `block_size`/`rounding_rule` for Block.
2. Rewrite `create_custom_pay_item` to write `PayItemRateSlots`, set `IsSlotBased = TRUE`, skip RateType creation.
3. Add method-specific slot validation functions (Section 7).

**Tests:** Write `tests/test_custom_item_slots.py` (new). Update `tests/test_settings_custom_pay_items.py`.

**Validation:** Create one item of each method via API. Confirm `PayItemRateSlots` rows created. Confirm no `RateTypes` created.

**Risk:** Medium. Core creation path changes. System items are unaffected.

**Rollback:** Revert service code. Schema migrations remain applied.

---

### Phase 4 — Custom Driver Rate API

**Objective:** Implement the `/payroll/driver-item-rates` endpoint family.

**Files/services:**
- New `app/payroll/item_rate_service.py`
- New route registration in the router
- New Pydantic schemas: `ItemRateVersionCreate`, `ItemRateVersionUpdate`, `ItemRateVersionResponse`, `ItemRateMatrixResponse`

**Entry condition:** Phase 3 complete.

**Work:** Implement all endpoints from Section 6. Implement all approval lifecycle functions from Section 5 for the slot-based path.

**Tests:** Write `tests/test_custom_rate_versions.py` (new).

**Risk:** Low — new routes, no existing routes modified.

---

### Phase 5 — Calculation Engine and Draft Evidence

**Objective:** Implement `_compute_slot_based`, adapt existing system calculation functions to return evidence, and update `_refresh_draft_calculations` to write evidence columns for all paths.

**Files/services:**
- `app/payroll/service.py` — new `_compute_slot_based`, update `_compute_calculated_amount` dispatch, adapt existing `_compute_*` functions (return contract only), update `_refresh_draft_calculations`

**Entry condition:** Phase 4 complete.

**Work:**
1. Implement `_compute_slot_based` (Section 8) with all five behaviors.
2. Add `IsSlotBased` dispatch in `_compute_calculated_amount`.
3. Adapt existing `_compute_per_unit`, `_compute_ordinal_tier`, `_compute_range_bracket`, `_compute_range_progressive`, `_compute_block` to return `(amount, nmr, driver_rate_id, breakdown)`. Formulas are unchanged.
4. Update `_refresh_draft_calculations` to: (a) write the four evidence columns atomically for all paths, (b) add `_FROZEN_CALCULATION_STATUSES` guard.

**Tests:** Write `tests/test_custom_rate_calculation.py` (new). Write `tests/test_system_rate_evidence.py` (new) to confirm system lines also write evidence columns.

**Risk:** Medium — touches the core calculation loop. System items must be fully regression-tested.

---

### Phase 6 — Review Freeze and Stale Behavior

**Objective:** Implement the staleness mechanism and frozen-period guard.

**Files/services:**
- `app/payroll/service.py` — `approve_rate` (stale detection), `void_rate` (stale detection), `finalize_period` (remove `_refresh_draft_calculations` call, add 5-point validation)

**Entry condition:** Phase 5 complete.

**Work:**
1. Add stale detection to `approve_rate` and `void_rate` using `IN ('InReview', 'Approved')` filter.
2. Replace `_refresh_draft_calculations` call in `finalize_period` with 5-point pre-finalization validation.

**Tests:** Write `tests/test_review_freeze.py` (new).

**Risk:** High — modifies `finalize_period`. Test thoroughly on staging.

---

### Phase 7 — Finalization and Snapshots

**Objective:** Update finalization INSERT SELECT for custom slot-based items.

**Files/services:**
- `app/payroll/service.py` — finalization INSERT SELECT, `SourceSnapshot` JSONB build

**Entry condition:** Phase 6 complete. Audit of `RateTypeID IS NOT NULL` assumptions in reports/ledger complete.

**Work:**
1. Branch the finalization SELECT for `IsSlotBased = TRUE` items (use `dl.ResolvedDriverRateID`, `dl.CalculationBreakdown`, no LATERAL rate re-resolution).
2. Update `SourceSnapshot` JSONB (Section 12).
3. Fix any report/ledger queries that assume `RateTypeID IS NOT NULL` or `ResolvedRateAmount IS NOT NULL`.

**Tests:** Write `tests/test_custom_rate_finalization.py` (new).

**Risk:** High — the finalization INSERT is the most critical query. Do not deploy without fully passing finalization tests.

---

### Phase 8 — Frontend Integration

**Objective:** Update UI to use the new slot-based Pay Item creation and Pay Rates endpoints.

**Files/services:** Frontend files.

**Entry condition:** Phase 7 complete, all backend tests green.

**Risk:** Medium. UI integration with new API shapes.

---

### Phase 9 — Full Regression

**Objective:** Confirm the full test suite is green. Manual end-to-end verification.

**Validation commands:**
```bash
pytest backend/tests/ --tb=short
```

Manual verification:
- Create one custom item per method.
- Enter driver rates for each method.
- Enter payroll data and confirm calculation.
- Transition to InReview. Approve a new rate. Confirm stale marking.
- Return to Open, recalculate, re-review, transition to Approved.
- Finalize. Confirm snapshot.
- Confirm system item payroll is unaffected.
- Confirm system item evidence columns are written.

---

### Phase 10 — Optional Future Cleanup

Deprecate and eventually drop `DriverRateTiers` once all system tiered behaviors are confirmed to use `PayItemRateSlots` (if/when system items are migrated to the new model). This is out of scope for the current plan.

---

## Section 18 — Complete Test Plan

### Existing Test Files to Update

| File | Required changes |
|---|---|
| `tests/test_settings_custom_pay_items.py` | Remove assertions about `CPI_` RateTypes created. Add assertions that `PayItemRateSlots` rows exist after creation. Remove `TestPayItemRateStructureBackfill` class. Update creation tests for all 5 methods. |
| `tests/test_pay_rates.py` | Remove N-groups-per-N-rate-names assertions. Update matrix response shape (one group per item, not N groups). Remove `test_backfill_repairs_broken_item`. Add tests for new `/payroll/driver-item-rates/matrix` endpoint. |
| `tests/test_m13c.py` | Rewrite fixture setup: replace `_create_custom_item + _link_rate_type` with `POST /settings/pay-items` (new creation path) + `POST /payroll/driver-item-rates/{driver_id}/{pay_item_id}/versions`. All calculation assertions retain the same expected values. |

### Existing Test Files That Must Pass Unchanged

- `tests/test_finalize.py` — system item finalization
- `tests/test_finalization_preview.py`
- `tests/test_payroll_period.py`
- `tests/test_db_integrity.py`
- `tests/test_cp5_calc_consistency.py`
- All system item rate tests in `tests/test_pay_rates.py`

### New Test Files

#### `tests/test_custom_item_slots.py` — Slot structural validation

- `test_create_perunit_creates_one_slot`
- `test_create_perunit_extra_slots_rejected`
- `test_create_ordinal_creates_n_slots_in_order`
- `test_create_ordinal_all_slots_have_is_open_ended_false` — verifies no OrdinalTier slot has IsOpenEnded = TRUE, including the final slot
- `test_create_ordinal_decimal_quantity_rejected_at_entry`
- `test_create_range_requires_min_two_slots`
- `test_create_range_first_slot_must_start_at_zero`
- `test_create_range_exact_continuity_required` — gap rejected, overlap rejected
- `test_create_range_only_final_slot_open_ended`
- `test_create_block_creates_one_slot_with_block_config_on_item`
- `test_create_block_missing_block_size_rejected`
- `test_create_block_invalid_rounding_rule_rejected`
- `test_no_ratetype_created_for_custom_item`
- `test_no_payitemratetypemap_created_for_custom_item`
- `test_no_rate_name_settings_created_for_custom_item`
- `test_duplicate_slot_index_rejected`
- `test_slot_name_empty_rejected`
- `test_slot_mutation_blocked_when_pending_rate_exists`
- `test_slot_mutation_blocked_when_approved_rate_exists`
- `test_slot_mutation_blocked_when_draft_line_references_item`
- `test_slot_mutation_blocked_when_final_line_references_item`
- `test_slot_name_rename_allowed_even_when_in_use`
- `test_slot_delete_blocked_when_in_use` — verifies trigger raises for blocked delete; verifies trigger returns OLD for allowed delete (does not cancel delete)
- `test_slot_delete_cascade_from_driverrateslotvalues_fk` — verifies ON DELETE RESTRICT on PayItemRateSlotID prevents slot deletion while slot values exist
- `test_block_config_null_for_non_block_item`
- `test_rate_behavior_change_blocked_when_in_use`
- `test_system_item_block_config_not_constrained_by_payitems` — confirms existing system Block items with BlockSize/RoundingRule on DriverRates remain valid after migration 0044 (IsSlotBased = FALSE satisfies ck_PayItems_BlockConfig)

#### `tests/test_custom_rate_versions.py` — Rate version lifecycle

- `test_create_version_requires_all_slots`
- `test_create_version_partial_slots_rejected`
- `test_create_version_unknown_slot_id_rejected`
- `test_create_version_retired_slot_rejected`
- `test_create_version_duplicate_slot_rejected`
- `test_create_second_pending_version_returns_409_with_existing_id`
- `test_update_pending_replaces_all_slot_values_atomically`
- `test_update_approved_not_permitted`
- `test_approve_validates_completeness`
- `test_approve_supersedes_prior_approved`
- `test_approve_sets_prior_effective_to`
- `test_approve_blocked_if_future_approved_exists`
- `test_effective_date_overlap_rejected_by_gist_constraint`
- `test_void_full_version`
- `test_void_blocked_if_referenced_in_final_lines`
- `test_copy_creates_pending_with_all_slot_values`
- `test_copy_cross_company_rejected`
- `test_copy_cross_branch_creates_independent_version`
- `test_change_one_slot_creates_new_complete_version`
- `test_branch_a_rate_does_not_resolve_for_branch_b_line`
- `test_same_driver_independent_approved_versions_per_branch`
- `test_driverrateslotvalues_cascade_delete_when_parent_driverrate_deleted` — verifies ON DELETE CASCADE

#### `tests/test_custom_rate_calculation.py` — Calculation engine (custom slot-based)

- `test_perunit_calculation`
- `test_ordinal_qty_1` → $35.0000
- `test_ordinal_qty_4` → $35 + $30 + $25 + $25 = $115.0000
- `test_ordinal_final_slot_catches_positions_above_max_index` — verifies highest SlotIndex rate applied without IsOpenEnded
- `test_ordinal_decimal_quantity_returns_nmr`
- `test_range_bracket_qty_in_tier_1` (qty=5, slots [0,10) [10,20) [20,∞))
- `test_range_bracket_qty_exactly_at_boundary` (qty=10 → Slot 2)
- `test_range_bracket_open_ended_tier` (qty=25 → Slot 3)
- `test_range_progressive_within_first_tier`
- `test_range_progressive_spanning_two_tiers`
- `test_range_progressive_spanning_all_tiers` (qty=25 → $45.0000)
- `test_block_floor_rounding`
- `test_block_ceiling_rounding`
- `test_block_nearest_half_up`
- `test_block_zero_blocks_valid` (not NMR)
- `test_no_approved_rate_returns_nmr`
- `test_pending_only_rate_returns_nmr`
- `test_missing_slot_value_returns_nmr`
- `test_voided_rate_returns_nmr`
- `test_block_config_read_from_pay_item_not_driver_rate`
- `test_calculation_breakdown_written_atomically_with_calculated_amount`
- `test_resolved_driver_rate_id_written_atomically`

#### `tests/test_system_rate_evidence.py` — System item evidence columns (new, Phase 5)

- `test_system_perunit_writes_resolved_driver_rate_id`
- `test_system_perunit_writes_calculation_breakdown`
- `test_system_perunit_calculation_breakdown_contains_rate_type_code`
- `test_system_tiered_writes_breakdown_with_tier_detail`
- `test_system_rate_stale_marked_when_rate_superseded_in_inreview_period`
- `test_system_rate_stale_marked_when_rate_superseded_in_approved_period`
- `test_system_rate_stale_marked_when_rate_voided_in_inreview_period`
- `test_system_finalization_uses_stored_evidence_not_recalculated`

#### `tests/test_review_freeze.py` — Frozen-period freeze and stale handling

- `test_approve_rate_marks_inreview_lines_stale`
- `test_approve_rate_marks_approved_period_lines_stale`
- `test_void_rate_marks_inreview_lines_stale`
- `test_void_rate_marks_approved_period_lines_stale`
- `test_finalization_blocked_when_stale_line_exists`
- `test_stale_resolves_after_return_to_open_and_recalculate`
- `test_refresh_draft_calculations_raises_for_inreview_period`
- `test_refresh_draft_calculations_raises_for_approved_period`
- `test_finalize_does_not_call_refresh_draft_calculations`
- `test_inreview_calculations_not_changed_by_new_rate_approval`
- `test_approved_period_calculations_not_changed_by_new_rate_approval`

#### `tests/test_custom_rate_finalization.py` — Finalization evidence

- `test_finalize_slot_based_item_sets_ratetypeid_null`
- `test_finalize_slot_based_item_uses_resolved_driver_rate_id`
- `test_snapshot_contains_calculation_breakdown`
- `test_snapshot_calculation_total_matches_final_amount`
- `test_snapshot_slot_names_preserved_from_review_time`
- `test_snapshot_unaffected_by_slot_rename_after_finalize`
- `test_finalize_uses_stored_calculated_amount_not_recalculated`
- `test_finalized_slot_values_immutable_after_finalization`
- `test_system_item_finalization_unchanged` — regression
- `test_finalization_5point_validation_blocks_on_stale`
- `test_finalization_5point_validation_blocks_on_null_resolved_rate_id`
- `test_finalization_5point_validation_blocks_on_voided_rate`
- `test_finalization_5point_validation_blocks_on_null_breakdown`

---

## Section 19 — Risks and Safeguards

| Risk | Mitigation |
|---|---|
| **Migration 0044 on active DriverRates table** | Run in a transaction. Test on staging copy. Schedule during low-traffic window. The changes are additive (add column, drop NOT NULL). |
| **Nullable RateTypeID breaks code paths that assume NOT NULL** | Audit all service code for `WHERE ratetypeid IS NOT NULL` guards. The `ck_DriverRates_ItemOrType` constraint ensures either RateTypeID or PayItemID is always set. |
| **Old code paths assuming RateTypeID on DriverRates** | Phase 3 (new creation path) is deployed simultaneously with Phase 2 (cleanup). No new CPI_ rows created after cleanup. Existing system-item code unchanged. |
| **Report queries assuming ResolvedRateAmount IS NOT NULL** | Audit in Phase 7 before deployment. Add NULL guards or redirect to FinalAmount. |
| **Cleanup migration deletes real data** | Guard 0 in migration 0045 aborts if any custom PayItem rows exist. Four additional guards abort if unexpected related data exists. Pre-migration audit confirms zero. |
| **Race condition: two pending versions created simultaneously** | `ux_DriverRates_Driver_PayItem_Pending` partial unique index causes one INSERT to fail with unique violation. Service-layer 409 check runs before the INSERT. |
| **Overlapping approved date ranges** | `excl_DriverRates_PayItem_no_overlap` GIST exclusion constraint prevents this at the DB level. |
| **Wrong branch rate resolving for a payroll line** | BranchID included in all resolution queries. No cross-branch resolution possible. |
| **Stale data silently finalized** | Pre-finalization stale check (Phase 6) blocks finalization entirely while any non-void line is stale. |
| **Incomplete slot set approved** | `approve_rate` validates complete slot set before acquiring the advisory lock. |
| **Structural slot mutation after use** | DB trigger `trg_guard_payitemrateslot_structural_mutation` blocks at the database level. Service-layer pre-check provides human-readable error first. |
| **Frontend/API mismatch** | New endpoints have a distinct route family (`/driver-item-rates`). Old endpoints unchanged. Mismatch produces 404, not a silent error. |
| **Downgrade after slot-based data exists** | Downgrade script validates zero PayItemID rows before restoring NOT NULL constraints. Cannot downgrade if slot-based rates have been created. |
| **CalculationBreakdown and ResolvedDriverRateID out of sync** | Written in the same `UPDATE` statement in `_refresh_draft_calculations` for all paths. Cannot be out of sync from application code. |
| **Finalization recalculating at a different rate than reviewed** | `_refresh_draft_calculations` removed from `finalize_period`. Finalization uses `dl.ResolvedDriverRateID` directly. Frozen-period guard prevents refresh for InReview and Approved periods. |
| **System item lines missing evidence columns** | Phase 5 adapts existing `_compute_*` functions to return `(driver_rate_id, breakdown)`. `_refresh_draft_calculations` writes evidence for all paths. `tests/test_system_rate_evidence.py` confirms. |
| **Trigger installed in 0043 queries missing PayItemID column** | PayItemRateSlots and PayItems structural triggers are installed in 0044 (after `DriverRates.PayItemID` exists). Only the `DriverRateSlotValues` mutation trigger (which does not query PayItemID) is installed in 0043. |
| **Slot deletion orphaning slot values** | `ON DELETE RESTRICT` FK on `DriverRateSlotValues.PayItemRateSlotID` prevents physical slot deletion while values reference it. Structural mutation trigger provides the service-layer error first. |
| **IsOpenEnded = TRUE on OrdinalTier slot stored in DB** | DB constraint `CHECK (NOT IsOpenEnded OR ToUnit IS NULL)` permits this combination but service validation rejects it. Tests in `test_custom_item_slots.py` confirm all OrdinalTier slots are stored with `IsOpenEnded = FALSE`. |

---

## Section 20 — Final Decisions Summary

| # | Decision | Answer |
|---|---|---|
| 1 | What owns slot definitions? | `payroll.PayItemRateSlots` — one row per slot per Pay Item, ordered by SlotIndex. |
| 2 | What owns slot monetary values? | `payroll.DriverRateSlotValues` — one row per slot per Driver Rate version. |
| 3 | What is the rate-version parent? | `payroll.DriverRates` — reused as-is with `PayItemID` added. |
| 4 | Do custom items create RateTypes? | No. New custom items create no RateType, PayItemRateTypeMap, or rate_name_N PayItemSettings rows. |
| 5 | What is the effective-dating identity? | `(CompanyID, BranchID, DriverID, PayItemID, effective date range)`. |
| 6 | How many Pending versions are allowed? | At most one PendingApproval per `(CompanyID, BranchID, DriverID, PayItemID)`, enforced by partial unique index. |
| 7 | What happens when one slot changes? | A new complete PendingApproval version is created with all slot values. Unchanged values are copied forward. The new version is approved and supersedes the prior version. |
| 8 | Where do BlockSize and RoundingRule live? | On `PayItems` (structural, shared by all drivers) for custom slot-based Block items. Existing system Block items continue using `DriverRates.BlockSize` and `DriverRates.RoundingRule` — unchanged. |
| 9 | What range-boundary convention is used? | Half-open `[FromUnit, ToUnit)`. `ToUnit` is the exclusive upper bound. The final slot of a range method has `ToUnit = NULL` and `IsOpenEnded = TRUE`, representing `[FromUnit, ∞)`. PerUnit, OrdinalTier, and Block slots have `ToUnit = NULL` and `IsOpenEnded = FALSE`. |
| 10 | Does `IsOpenEnded` apply to OrdinalTier slots? | No. `IsOpenEnded = FALSE` on every OrdinalTier slot, including the final slot. The calculation engine applies the highest-`SlotIndex` rate to all ordinal positions at or above that index without reading `IsOpenEnded`. |
| 11 | What are the frozen calculation states? | `_FROZEN_CALCULATION_STATUSES = frozenset({'InReview', 'Approved'})`. Both states freeze calculation evidence. `_refresh_draft_calculations` is blocked in both states. Stale detection runs for periods in either state. |
| 12 | When do calculations freeze? | At the Open → InReview transition. `_refresh_draft_calculations` runs once, writes evidence for all rate-requiring lines (system and custom), and then the frozen-period guard prevents further refresh for InReview and Approved periods. |
| 13 | What happens when a rate changes or is voided during a frozen period? | Affected draft lines are marked `IsStale = TRUE`. Finalization is blocked. The period must return to Open, recalculate, and re-enter review before finalizing. |
| 14 | Does finalization recalculate? | No. Finalization validates and copies stored evidence. `_refresh_draft_calculations` is not called. `FinalAmount = dl.CalculatedAmount`. |
| 15 | Do system item lines write evidence columns? | Yes. After Phase 5, all rate-driven lines (system and custom) write `ResolvedDriverRateID`, `CalculationBreakdown`, `CalculatedAtUtc`, and `IsStale = FALSE` atomically. System functions require return-contract adaptation only — formulas are unchanged. |
| 16 | What is the FK deletion behavior for `DriverRateSlotValues`? | `DriverRateID`: `ON DELETE CASCADE` — slot values are deleted automatically when their parent DriverRate is physically deleted (guarded by the finalization trigger). `PayItemRateSlotID`: `ON DELETE RESTRICT` — a slot cannot be deleted while slot values reference it. |
| 17 | When are structural triggers installed? | Migration 0044. Both `trg_guard_payitemrateslot_structural_mutation` and `trg_guard_payitem_structural_mutation` depend on `DriverRates.PayItemID` and cannot be installed in migration 0043. |
| 18 | What is the first implementation step? | Phase 0: Run all 8 audit SQL queries on the production database. Confirm all return zero. Record database name, timestamp, and each count. |
| 19 | What decisions, if any, are genuinely still unresolved? | **Open decision 1:** Maximum allowed slot count for OrdinalTier and Range methods. Recommended constants: `ORDINAL_TIER_MAX_SLOTS = 20`, `RANGE_MAX_SLOTS = 20`. These are validation constants, not DB constraints. The product owner should confirm whether these limits are correct for the expected use cases. **Open decision 2:** Whether `DriverRateTiers` should eventually be deprecated for all system tiered behaviors as well (bringing system items to the same slot model). This would be a large future project and is explicitly out of scope. No decision is required before this plan is implemented. |

---

*End of document.*
