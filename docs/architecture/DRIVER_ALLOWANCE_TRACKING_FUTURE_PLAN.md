# Driver Allowance Tracking — Future Architecture Plan (v4)

**Project:** Payroll App v3 (Flussra) · **Codename:** DAC (Driver Allowance Categories)
**Status:** Accepted future plan — not an active implementation task
**Revision:** v4 — final accepted after three Codex review passes

---

> **⚠ FUTURE PLAN ONLY**
>
> This document is an accepted future architecture plan only. It must not be treated as an implementation task. Implementation should happen only after the core payroll app is stable and after a dedicated DAC phase is explicitly approved.

---

## Table of Contents

1. [Executive Recommendation](#1-executive-recommendation)
2. [Current System Findings](#2-current-system-findings)
3. [Corrected Product Relationship](#3-corrected-product-relationship)
4. [Corrected Backend Model](#4-corrected-backend-model)
5. [Status Entry Channel Decision](#5-status-entry-channel-decision)
6. [Future StatusKeyPayRule Separation](#6-future-statuskeypay-rule-separation)
7. [Corrected Ledger Strategy](#7-corrected-ledger-strategy)
8. [Draft / Open-Period Usage Strategy](#8-draft--open-period-usage-strategy)
9. [Finalized Ledger Strategy](#9-finalized-ledger-strategy)
10. [Idempotency and Double-Count Protection](#10-idempotency-and-double-count-protection)
11. [Cycle Strategy](#11-cycle-strategy)
12. [Branch Config Strategy](#12-branch-config-strategy)
13. [Versioning, Effective-Date, and No-Overlap Strategy](#13-versioning-effective-date-and-no-overlap-strategy)
14. [Status-Key Relational Link Strategy](#14-status-key-relational-link-strategy)
15. [Driver Entitlement Strategy](#15-driver-entitlement-strategy)
16. [Adjustment / Reset Governance](#16-adjustment--reset-governance)
17. [Migration / Backfill Strategy](#17-migration--backfill-strategy)
18. [Tenant Integrity and Permission / Security Model](#18-tenant-integrity-and-permission--security-model)
19. [Risks and Tradeoffs](#19-risks-and-tradeoffs)
20. [Future Phase Plan](#20-future-phase-plan)
21. [Files and Code Areas Considered](#21-files-and-code-areas-considered)
22. [Final Verdict](#22-final-verdict)

---

## 1. Executive Recommendation

**Backend-first. The concept is sound. v4 is the final accepted revision.**

This document incorporates all corrections from three Codex review passes:

- Status is a **System Status Entry Channel**, not a PayItem
- Existing `PTO_STATUS` is a separate seeded PayItem; must not be confused with the Day Grid Status Entry Channel
- `StatusKeyPayRule` reserved as a future separate concept
- `NineMonths` cycle type deferred
- Ledger uses positive `HoursAmount` + `EntryDirection`; no negative amounts
- Open-period usage derived from draft lines, not written to the ledger
- DAC-5 is user-visible, feature-flagged, and high-risk
- Effective-dated tables require explicit no-overlap protection
- Tenant integrity requires DB-level composite FK patterns where feasible, plus service-layer guards on all FK dimensions
- Old `PayrollStatusKeys.AllowanceCategory` text column retained as shadow until DAC-9; `CategoryCode ≤ 50` enforced while it exists; NULL relaxation is not permitted
- Ledger snapshot columns are NOT NULL only for `FinalizedLine/Usage` rows; nullable for adjustment/reset/correction rows
- Frontend Driver Center remains future-only

No phase order changes. The ten-phase plan is the approved sequence.

---

## 2. Current System Findings

> **Schema name note:** Table references use illustrative schema prefixes (`payroll.*`). The project's actual schemas are `core`, `sec`, and `payroll` as found in the current migration files. When this plan is implemented, all table references must be resolved against confirmed migration-file schema names. References to `app.companies`, `app.users`, `app.branches`, `app.drivers` in FK examples are placeholders that must be resolved from the migration history before implementation.

### 2.1 Status Key storage

`payroll.PayrollStatusKeys` is the current home of all status-related configuration.

| Column | Type | Current behavior |
|---|---|---|
| `StatusCode` | `VARCHAR(60)` | Auto-assigned `SK_XXXXX`; stable identifier |
| `KeyName` | `VARCHAR(200)` | User-facing label (added in migration `0018`) |
| `HoursValue` | `NUMERIC(5,2) DEFAULT 0` | Metadata only — loaded for UI; **not applied in any calculation** |
| `IsOffReason` | `BOOLEAN DEFAULT TRUE` | Whether the status represents an absence/off day |
| `DeductsFromYearlyAllowance` | `BOOLEAN DEFAULT FALSE` | Future intent flag; **not used in any calculation path today** |
| `AllowanceCategory` | `VARCHAR(50)` | Free-text from hardcoded set; **not used in finalization or ledger** |

Existing DB-level constraints:
- `AllowanceCategory` must be non-null if `DeductsFromYearlyAllowance = TRUE`
- `DeductsFromYearlyAllowance = TRUE` requires `IsOffReason = TRUE`

### 2.2 Hardcoded allowance category set

`backend/app/settings/schemas.py`:

```python
_ALLOWANCE_CATEGORIES = {
    "Vacation", "Sick", "Bereavement", "Jury Duty", "Personal", "Other",
}
```

No relational table. No company-owned catalog. No FK. Validated by a Pydantic field validator in `StatusKeyCreate`. In DAC-2, this hardcoded validator must be replaced with a company-category DB lookup.

### 2.3 DailyStatus line storage

Day-grid save path (`payroll/service.py` ~lines 9067–9085) writes `PayrollDraftLines`:

| Column | Value |
|---|---|
| `LineType` | `'DailyStatus'` |
| `LineScope` | `'Daily'` |
| `Quantity` | `0` |
| `RateAmount` | `NULL` |
| `Notes` | status key code string (e.g. `SK_00042`) |
| `FinalAmount` | `0` (at finalization) |

The status key code in `Notes` is the only current link back to `PayrollStatusKeys`. No `StatusKeyID` FK. No `AllowanceCategoryID`.

### 2.4 HoursValue — confirmed metadata only

`HoursValue` is loaded into the `DayGridStatusKey` response for UI display only. It is never applied in any payroll calculation or finalization path.

### 2.5 Finalization behavior for DailyStatus lines

All non-Void draft lines, including `DailyStatus` lines, are copied to `PayrollFinalLines` at period lock. For status lines: `FinalAmount = 0`, `ResolvedRateAmount = NULL`, `PayItemID = NULL`. `AllowanceCategory` and `DeductsFromYearlyAllowance` are never read during finalization.

### 2.6 Usage limits — confirmed separate concern

Four independent usage-limit types exist on `PayrollStatusKeys` (migration `0018`). Enforced in `_enforce_status_key_limits()` (~lines 8261–8454 of `payroll/service.py`). Entirely separate from allowance entitlement deduction. Must not be conflated with DAC.

### 2.7 PTO_STATUS and the Status Entry Channel

`0003_pay_items_seed.sql` seeds standard pay items including `PTO_STATUS`. This is a seeded `PayItems` row that participates in pay item configuration, rate mapping, and finalization like any other pay item. **It is not the Day Grid Status Entry Channel.**

The Day Grid Status column is implemented through `LineType = 'DailyStatus'` in `PayrollDraftLines`, independent of any `PayItems` row. There is no `PayItems` row that owns or controls the Day Grid Status column. `PTO_STATUS` as a pay item and the Status Entry Channel are distinct concepts and must not be conflated. See §5 for full rationale.

---

## 3. Corrected Product Relationship

### 3.1 System Status Entry Channel

**What it is:** A system-level guarantee that the Status column exists in the Day Grid / payroll entry for every company and branch. It is not a pay item. It is operational metadata infrastructure.

**What it must not become:** A `PayItems` row. Adding Status as a pay item would pollute the PayItems catalog, BranchPayItemConfig activation, Pay Rates, PayItemRateTypeMap, finalization money logic, and all payroll money reports. These systems deal with monetary pay lines. Status entries carry `FinalAmount = 0` and have no rate. Mixing them into the pay item model creates category confusion in every downstream system.

**Properties:**
- The Status column always exists in the Day Grid. It cannot be deactivated.
- Branch and company setup may control which Status Keys are available, but neither may remove the Status column capability.
- Values in the Status column must come from Status Keys only — no free text.
- The System Status Entry Channel is an architectural invariant, enforced at the application layer.

### 3.2 Status column

The per-driver, per-day column in the Day Grid where a payroll user selects a status. Stored as a `DailyStatus` line in `PayrollDraftLines` where `Notes = status_key_code`. Always derived from a valid Status Key.

### 3.3 Status Keys

The selectable values available in the Status column. Each Status Key is a named status with independently configurable behavior in two future domains:

1. **Allowance deduction behavior** — addressed by DAC
2. **Payroll payment behavior** — reserved for future `StatusKeyPayRule` (§6)

These two domains must remain separate.

### 3.4 Allowance Categories

Company-owned allowance buckets (e.g. Vacation, Sick, Jury Duty, Bereavement, Personal, Other).

**What Allowance Categories are not:** They are not a source of payroll payment rates. They are not selectable by payroll users. They do not appear in the Day Grid. They are purely the accounting destination of an allowance deduction. A Status Key points to a category; the category does not define payment behavior.

### 3.5 Future `StatusKeyPayRule` placeholder

Reserved for a future phase. Different Status Keys require different payment rules: Sick may pay at a sick-rate or regular-rate; Vacation/PTO may have its own PTO pay rule; Jury Duty may have a mandated daily amount; Unpaid Off pays nothing. No single generic rate covers all cases. See §6.

### 3.6 Driver Entitlements

Per-driver, per-category, per-cycle capacity records. Each driver has their own bucket. No shared company pool.

### 3.7 Allowance Usage

Record of how much of an entitlement a driver has consumed. For open periods: derived from active draft lines. For locked periods: written to the immutable `AllowanceUsageLedger`.

### 3.8 The complete chain

```
Day Grid / Payroll Entry
  → Status column (guaranteed by the System Status Entry Channel invariant)
  → user selects a Status Key (e.g. "Vacation Day")
  → Status Key has two independent optional future effects:

      1. PAYROLL PAYMENT BEHAVIOR (future — reserved StatusKeyPayRule)
         Status Key → StatusKeyPayRule → paid hours / pay rate rule
         (not implemented in DAC; reserved so DAC does not block it)

      2. ALLOWANCE DEDUCTION BEHAVIOR (DAC)
         Status Key → DeductsFromYearlyAllowance = TRUE
         → links to AllowanceCategory (e.g. "Vacation")
         → DeductionAmountType resolves deduction hours
         → deduction applied to driver's entitlement for that category and cycle
         → usage recorded in AllowanceUsageLedger at finalization

These two effects are INDEPENDENT.
Deducted allowance hours ≠ paid payroll hours (necessarily).
AllowanceCategory is NOT the pay-rate source.
```

### 3.9 Single Status Column Clarification

There is **only one Day Grid Status column**.

The Status column is the single place where a payroll user selects a Status Key for a given driver/day combination. There is no second Status column, no parallel status input, and no separate column added by DAC or by the future `StatusKeyPayRule` concept.

Selecting one Status Key may later produce two independent downstream effects:

1. **Allowance usage behavior** — handled by DAC: the selected Status Key may be linked to an `AllowanceCategory` deduction rule, which triggers a debit against the driver's entitlement when the period is finalized.
2. **Payroll payment behavior** — reserved for the future `StatusKeyPayRule`: the selected Status Key may eventually link to a pay rule that drives how many hours are paid and at what rate.

These are rule layers attached to the Status Key — not additional UI columns or input fields. The user still selects exactly one Status Key in the one Status column. The downstream effects are evaluated in the backend.

The accepted model:

```
One Status column
  → one selected Status Key
  → optional allowance effect (DAC)
  → optional pay effect (future StatusKeyPayRule)
```

`StatusKeyPayRule` is not a column. It is a future backend rule layer for calculating payroll payment from the already-selected Status Key.

Allowance deduction rules are not a column. They are future/backend rules (in `StatusKeyAllowanceRules`) attached to the already-selected Status Key.

Neither concept introduces any new column, field, or user-facing input to the Day Grid.

---

## 4. Corrected Backend Model

> **Schema name note:** Schema prefixes below (e.g. `payroll.`) are illustrative. Use the project's actual schemas — currently `core`, `sec`, and `payroll` — as confirmed from current migration files.

### 4.1 `payroll.AllowanceCategories`

```sql
AllowanceCategoryID   BIGSERIAL PRIMARY KEY,
CompanyID             INTEGER NOT NULL REFERENCES [companies table],
CategoryCode          VARCHAR(60) NOT NULL,  -- max 50 while shadow column exists (§14.2)
CategoryName          VARCHAR(120) NOT NULL,
Description           TEXT,
IsActive              BOOLEAN NOT NULL DEFAULT TRUE,
DisplayOrder          INTEGER NOT NULL DEFAULT 100,
CreatedAtUtc          TIMESTAMPTZ NOT NULL DEFAULT now(),
UpdatedAtUtc          TIMESTAMPTZ NOT NULL DEFAULT now(),
CreatedByUserID       INTEGER REFERENCES [users table],

UNIQUE (CompanyID, CategoryCode),
UNIQUE (CompanyID, AllowanceCategoryID),  -- required for composite FK references
CHECK (CategoryCode = UPPER(TRIM(CategoryCode)))
```

The composite `UNIQUE (CompanyID, AllowanceCategoryID)` is required so other tables can enforce cross-company isolation via composite FKs.

### 4.2 `payroll.BranchAllowanceCategoryConfig`

Effective-dated branch overrides. If no row exists for a branch/category, the branch inherits a company-level default resolved at the application layer.

```sql
ConfigID              BIGSERIAL PRIMARY KEY,
AllowanceCategoryID   BIGINT NOT NULL,
BranchID              INTEGER NOT NULL,
CompanyID             INTEGER NOT NULL,    -- denormalized; enforced at DB level
HoursPerAllowanceDay  NUMERIC(5,2),        -- NULL = inherit company default
IsActive              BOOLEAN NOT NULL DEFAULT TRUE,
EffectiveFrom         DATE NOT NULL,
EffectiveTo           DATE,               -- NULL = open
Notes                 TEXT,
CreatedAtUtc          TIMESTAMPTZ NOT NULL DEFAULT now(),
CreatedByUserID       INTEGER REFERENCES [users table],

-- Composite FKs for tenant integrity:
FOREIGN KEY (CompanyID, AllowanceCategoryID)
    REFERENCES payroll.AllowanceCategories(CompanyID, AllowanceCategoryID),
FOREIGN KEY (CompanyID, BranchID)
    REFERENCES [branches table](CompanyID, BranchID),
    -- Requires UNIQUE (CompanyID, BranchID) on branches; add if not present

UNIQUE (AllowanceCategoryID, BranchID, EffectiveFrom),
CHECK (EffectiveTo IS NULL OR EffectiveTo > EffectiveFrom),
CHECK (HoursPerAllowanceDay IS NULL OR HoursPerAllowanceDay > 0)
```

No-overlap protection: see §13.2.

### 4.3 `payroll.AllowanceCycles`

One cycle definition per company-category pair.

```sql
CycleID               BIGSERIAL PRIMARY KEY,
CompanyID             INTEGER NOT NULL,
AllowanceCategoryID   BIGINT NOT NULL,
CycleType             VARCHAR(30) NOT NULL,
CycleMonths           SMALLINT,            -- required for Custom only
AnchorMonth           SMALLINT NOT NULL DEFAULT 1,
AnchorDay             SMALLINT NOT NULL DEFAULT 1,
CreatedAtUtc          TIMESTAMPTZ NOT NULL DEFAULT now(),

FOREIGN KEY (CompanyID, AllowanceCategoryID)
    REFERENCES payroll.AllowanceCategories(CompanyID, AllowanceCategoryID),

UNIQUE (CompanyID, AllowanceCategoryID),
CHECK (CycleType IN ('Annual', 'HalfYear', 'Quarterly', 'Custom')),
CHECK (CycleType != 'Custom' OR (CycleMonths IS NOT NULL AND CycleMonths > 0)),
CHECK (AnchorMonth BETWEEN 1 AND 12),
CHECK (AnchorDay BETWEEN 1 AND 28)
```

`NineMonths` is **not included** in the initial enum — see §11.

### 4.4 `payroll.DriverAllowanceEntitlements`

```sql
EntitlementID         BIGSERIAL PRIMARY KEY,
CompanyID             INTEGER NOT NULL,
BranchID              INTEGER NOT NULL,
DriverID              INTEGER NOT NULL,
AllowanceCategoryID   BIGINT NOT NULL,
CycleStart            DATE NOT NULL,
CycleEnd              DATE NOT NULL,
AllowedHours          NUMERIC(8,2) NOT NULL,
Notes                 TEXT,
IsActive              BOOLEAN NOT NULL DEFAULT TRUE,
CreatedAtUtc          TIMESTAMPTZ NOT NULL DEFAULT now(),
CreatedByUserID       INTEGER REFERENCES [users table],

-- Composite FKs for tenant integrity:
FOREIGN KEY (CompanyID, AllowanceCategoryID)
    REFERENCES payroll.AllowanceCategories(CompanyID, AllowanceCategoryID),
FOREIGN KEY (CompanyID, BranchID)
    REFERENCES [branches table](CompanyID, BranchID),
FOREIGN KEY (CompanyID, DriverID)
    REFERENCES [drivers table](CompanyID, DriverID),
    -- Both require composite unique/PK on branches and drivers; see §18.2

UNIQUE (DriverID, AllowanceCategoryID, CycleStart),
CHECK (AllowedHours >= 0),
CHECK (CycleEnd > CycleStart)
```

`UsedHours` is NOT stored on this table. Always derived from `AllowanceUsageLedger`.

### 4.5 `payroll.AllowanceUsageLedger`

Immutable. Rows are never deleted.

```sql
UsageLedgerID                   BIGSERIAL PRIMARY KEY,
CompanyID                       INTEGER NOT NULL,
BranchID                        INTEGER NOT NULL,
DriverID                        INTEGER NOT NULL,
AllowanceCategoryID             BIGINT NOT NULL,
EntitlementID                   BIGINT,

-- Entry classification
EntryDirection                  VARCHAR(10) NOT NULL,   -- Debit | Credit
EntryType                       VARCHAR(20) NOT NULL,   -- Usage | Reversal | Adjustment | Reset | Correction
HoursAmount                     NUMERIC(8,2) NOT NULL,  -- always positive; direction by EntryDirection

-- Source traceability (nullable; source-specific constraints below)
WorkDate                        DATE,
PayrollPeriodID                 INTEGER,
DraftLineID                     BIGINT,
FinalLineID                     BIGINT,
AdjustmentRequestID             BIGINT,

-- Status key snapshots
-- NOT NULL only for FinalizedLine/Usage rows; nullable for adjustment/reset rows
StatusKeyID                     INTEGER,
StatusCodeSnapshot              VARCHAR(60),    -- NOT NULL when DeductionSource = 'FinalizedLine'
CategoryCodeSnapshot            VARCHAR(60),    -- NOT NULL when DeductionSource = 'FinalizedLine'
DeductionAmountTypeSnapshot     VARCHAR(30),
HoursPerAllowanceDaySnapshot    NUMERIC(5,2),
AllowanceRuleRevisionSnapshot   INTEGER,        -- revision of StatusKeyAllowanceRules at deduction time

-- Optional non-blocking audit enhancements (§7.3):
StatusKeyNameSnapshot           VARCHAR(200),   -- copy of KeyName at deduction time
CategoryNameSnapshot            VARCHAR(120),   -- copy of CategoryName at deduction time
BranchAllowanceCategoryConfigID BIGINT,         -- FK to config row active at deduction time

-- Source classification
DeductionSource                 VARCHAR(30) NOT NULL,
CreatedAtUtc                    TIMESTAMPTZ NOT NULL DEFAULT now(),

-- Composite FKs for tenant integrity:
FOREIGN KEY (CompanyID, AllowanceCategoryID)
    REFERENCES payroll.AllowanceCategories(CompanyID, AllowanceCategoryID),
FOREIGN KEY (CompanyID, DriverID)
    REFERENCES [drivers table](CompanyID, DriverID),
FOREIGN KEY (CompanyID, BranchID)
    REFERENCES [branches table](CompanyID, BranchID),
-- Simple FKs with service-layer company checks for lines and periods:
FOREIGN KEY (FinalLineID)   REFERENCES payroll.PayrollFinalLines(FinalLineID),
FOREIGN KEY (DraftLineID)   REFERENCES payroll.PayrollDraftLines(DraftLineID),
FOREIGN KEY (EntitlementID) REFERENCES payroll.DriverAllowanceEntitlements(EntitlementID),
FOREIGN KEY (AdjustmentRequestID)
    REFERENCES payroll.AllowanceAdjustmentRequests(AdjustmentRequestID),

CHECK (HoursAmount > 0),
CHECK (EntryDirection IN ('Debit', 'Credit')),
CHECK (EntryType IN ('Usage', 'Reversal', 'Adjustment', 'Reset', 'Correction')),
CHECK (
    (EntryType = 'Usage'      AND EntryDirection = 'Debit')
    OR (EntryType = 'Reversal'   AND EntryDirection = 'Credit')
    OR (EntryType = 'Adjustment' AND EntryDirection IN ('Debit', 'Credit'))
    OR (EntryType = 'Reset'      AND EntryDirection = 'Credit')
    OR (EntryType = 'Correction' AND EntryDirection IN ('Debit', 'Credit'))
),
CHECK (DeductionSource IN ('FinalizedLine', 'ApprovedAdjustment', 'PreDacInferred', 'PreDacUnknown'))
```

**Source-specific nullability constraints (service-layer enforced):**

| Source / EntryType | StatusCodeSnapshot | StatusKeyID | FinalLineID | AdjustmentRequestID | WorkDate |
|---|---|---|---|---|---|
| `FinalizedLine` + `Usage` | **Required** | **Required** | **Required** | — | **Required** |
| `ApprovedAdjustment` + `Adjustment/Reset/Correction` | Nullable | Nullable | — | **Required** | — |
| `PreDacInferred` / `PreDacUnknown` | Populated where determinable | Populated where determinable | **Required** | — | Populated where determinable |

`AllowanceCategoryID` and `EntitlementID` are required for all rows where the entry affects a specific entitlement. `CategoryCodeSnapshot` follows the same nullability as `StatusCodeSnapshot`.

**Balance formula:**

```
UsedHours = SUM(HoursAmount WHERE EntryDirection = 'Debit')
          - SUM(HoursAmount WHERE EntryDirection = 'Credit')
          WHERE EntitlementID = :id
```

### 4.6 `payroll.AllowanceAdjustmentRequests`

```sql
AdjustmentRequestID   BIGSERIAL PRIMARY KEY,
CompanyID             INTEGER NOT NULL,
BranchID              INTEGER NOT NULL,
DriverID              INTEGER NOT NULL,
AllowanceCategoryID   BIGINT NOT NULL,
EntitlementID         BIGINT,
RequestType           VARCHAR(30) NOT NULL,
RequestedByUserID     INTEGER NOT NULL,
RequestedAtUtc        TIMESTAMPTZ NOT NULL DEFAULT now(),
OldAllowedHours       NUMERIC(8,2),
NewAllowedHours       NUMERIC(8,2),
Reason                TEXT NOT NULL,
EffectiveDate         DATE NOT NULL,
Status                VARCHAR(20) NOT NULL DEFAULT 'Pending',
ReviewedByUserID      INTEGER,
ReviewedAtUtc         TIMESTAMPTZ,
ReviewNotes           TEXT,

-- Composite FKs for tenant integrity:
FOREIGN KEY (CompanyID, AllowanceCategoryID)
    REFERENCES payroll.AllowanceCategories(CompanyID, AllowanceCategoryID),
FOREIGN KEY (CompanyID, BranchID)
    REFERENCES [branches table](CompanyID, BranchID),
FOREIGN KEY (CompanyID, DriverID)
    REFERENCES [drivers table](CompanyID, DriverID),
FOREIGN KEY (EntitlementID)
    REFERENCES payroll.DriverAllowanceEntitlements(EntitlementID),
    -- CompanyID match on entitlement verified at service layer

CHECK (Status IN ('Pending', 'Approved', 'Rejected')),
CHECK (RequestType IN ('ResetCycle', 'AdjustHours', 'CorrectEntry')),
CHECK (length(trim(Reason)) > 0)
```

### 4.7 `payroll.StatusKeyAllowanceRules`

The allowance-relevant fields that are subject to effective-dating are versioned here, not as mutable columns on `PayrollStatusKeys`.

```sql
RuleID                          BIGSERIAL PRIMARY KEY,
StatusKeyID                     INTEGER NOT NULL,
CompanyID                       INTEGER NOT NULL,
Revision                        INTEGER NOT NULL DEFAULT 1,
AllowanceCategoryID             BIGINT,              -- NULL if DeductsFromYearlyAllowance = FALSE
DeductionAmountType             VARCHAR(30) NOT NULL DEFAULT 'FullDay',
FixedDeductionHours             NUMERIC(5,2),
AllowsMultiDayDeduction         BOOLEAN NOT NULL DEFAULT FALSE,
IsActive                        BOOLEAN NOT NULL DEFAULT TRUE,
EffectiveFrom                   DATE NOT NULL,
EffectiveTo                     DATE,
CreatedAtUtc                    TIMESTAMPTZ NOT NULL DEFAULT now(),
CreatedByUserID                 INTEGER REFERENCES [users table],
ChangeReason                    TEXT,

-- Composite FKs for tenant integrity:
FOREIGN KEY (CompanyID, AllowanceCategoryID)
    REFERENCES payroll.AllowanceCategories(CompanyID, AllowanceCategoryID),
FOREIGN KEY (CompanyID, StatusKeyID)
    REFERENCES payroll.PayrollStatusKeys(CompanyID, StatusKeyID),
    -- Requires UNIQUE (CompanyID, StatusKeyID) on PayrollStatusKeys (add in DAC-2)

UNIQUE (StatusKeyID, Revision),
UNIQUE (StatusKeyID, EffectiveFrom),
CHECK (EffectiveTo IS NULL OR EffectiveTo > EffectiveFrom),
CHECK (DeductionAmountType IN ('FullDay', 'HalfDay', 'FixedHours', 'EnteredHours')),
CHECK (DeductionAmountType != 'FixedHours' OR (FixedDeductionHours IS NOT NULL AND FixedDeductionHours > 0))
```

No-overlap protection: see §13.1.

### 4.8 Status Key additions on `PayrollStatusKeys`

Only non-versioned fields are added directly to `PayrollStatusKeys`:
- `AllowanceCategoryID BIGINT NULL FK` — denormalized convenience column; the versioned detail lives in `StatusKeyAllowanceRules`
- `UNIQUE (CompanyID, StatusKeyID)` must be added to support composite FK references from `StatusKeyAllowanceRules`

---

## 5. Status Entry Channel Decision

### Why Status must not model the Day Grid Status column as a PayItem

`PTO_STATUS` exists as a seeded `PayItems` row. It is a legitimate pay item used in pay configuration, BranchPayItemConfig activation, Pay Rates, PayItemRateTypeMap, and finalization money logic. It represents paying a driver at a PTO rate.

**The Day Grid Status column is a different thing.** It is implemented through `LineType = 'DailyStatus'` in `PayrollDraftLines`. No `PayItems` row owns or controls the Day Grid Status column.

**Correct statement:** There is no `PayItems` row that owns the Day Grid Status column. `PTO_STATUS` remains a separate pay item concept and must not be confused with the Status Entry Channel.

Adding any new PayItem intended to act as the Status column control mechanism would:

1. Pollute `PayItems`, `BranchPayItemConfig`, Pay Rates, `PayItemRateTypeMap`, and finalization money logic with a zero-amount special case
2. Require a permanent carve-out in `BranchPayItemConfig` to prevent the Status column from being deactivatable
3. Require filtering of the status row from every payroll summary and money report
4. Corrupt the clean `FinalAmount > 0` money model that the finalization path relies on

The `DailyStatus` line type already implements the Status column correctly. The System Status Entry Channel is an application-layer invariant:

1. The day-grid service always includes the Status column in grid construction regardless of branch pay item configuration.
2. Branch/company setup screens expose Status Key management but never a "disable Status column" toggle.

No schema change is required to implement this invariant. It is a behavioral contract.

---

## 6. Future StatusKeyPayRule Separation

### The problem this reserves space for

Different Status Keys will realistically require different payment behaviors:

| Status Key | Likely future pay behavior |
|---|---|
| Vacation Day | Pay X hours at PTO/vacation rate |
| Sick Day | Pay X hours at sick-rate or regular-rate (company policy dependent) |
| Jury Duty | Pay a company-defined daily amount or match regular rate |
| Bereavement | Pay X hours per company bereavement policy |
| Unpaid Off | Pay nothing |
| Personal Day | Pay at regular rate or PTO rate depending on company |

### Reserved concept: `StatusKeyPayRules`

```sql
-- FUTURE CONCEPT — NOT IMPLEMENTED IN ANY DAC PHASE
-- Reserved here to ensure the allowance architecture does not block it

StatusKeyPayRuleID     BIGSERIAL PK,
StatusKeyID            INTEGER FK → PayrollStatusKeys,
PayRuleType            VARCHAR(30),   -- FixedHours | EnteredHours | NoPayment | etc.
PaidHours              NUMERIC(5,2),  -- if FixedHours
RateTypeID             INTEGER FK,    -- which rate type drives the pay amount
EffectiveFrom          DATE,
EffectiveTo            DATE
```

### What DAC must not do to block this

1. Must not add `PaidHours` or `RateTypeID` to `AllowanceCategories`
2. Must not assume `AllowanceDeductionHours == PaidPayrollHours`
3. Must not hardcode "if status key deducts, then it also pays" logic anywhere
4. `AllowanceUsageLedger` must not include `PaidAmount` or `RateAmount` fields
5. Future `StatusKeyPayRules` must be implementable without modifying `AllowanceCategories` or `AllowanceUsageLedger`

> **Allowance deduction and payroll payment are independently configurable per Status Key. A Status Key is the bridge. The bridge has two lanes. DAC builds one lane; StatusKeyPayRule builds the other. Neither lane carries traffic from both.**

---

## 7. Corrected Ledger Strategy

### 7.1 Ledger model summary

All amounts are positive. Direction is carried by `EntryDirection`, not by negative values.

| Entry scenario | EntryDirection | EntryType | HoursAmount |
|---|---|---|---|
| Driver uses 8h vacation | Debit | Usage | 8.00 |
| That usage reversed (wrong key) | Credit | Reversal | 8.00 |
| Admin adjusts entitlement up 4h | Credit | Adjustment | 4.00 |
| Admin adjusts entitlement down 4h | Debit | Adjustment | 4.00 |
| Annual cycle reset | Credit | Reset | (current net used) |
| Backfill inferred from pre-DAC data | Debit | Usage | (estimated) |

### 7.2 Idempotency index

```sql
CREATE UNIQUE INDEX ux_AllowanceLedger_FinalLine
    ON payroll.AllowanceUsageLedger (FinalLineID)
    WHERE FinalLineID IS NOT NULL
      AND EntryType = 'Usage'
      AND DeductionSource = 'FinalizedLine';
```

### 7.3 Optional non-blocking audit snapshot improvements

These columns are **optional but recommended from the first ledger migration (DAC-6) if practical**:

- `StatusKeyNameSnapshot VARCHAR(200)` — copy of `KeyName` at deduction time; useful if a key is later renamed or retired
- `CategoryNameSnapshot VARCHAR(120)` — copy of `CategoryName` at deduction time
- `BranchAllowanceCategoryConfigID BIGINT` — FK to the effective-dated config row active at deduction time

These are non-blocking. `HoursPerAllowanceDaySnapshot` already captures the essential numeric value. If deferred from DAC-6, they can be added in DAC-8 without correctness risk.

---

## 8. Draft / Open-Period Usage Strategy

For **open periods** (any status other than Locked/Archived): do not write `AllowanceUsageLedger` rows. Derive usage from active `DailyStatus` draft lines:

```sql
SELECT
    dl.driverid,
    sar.AllowanceCategoryID,
    SUM(resolve_deduction_hours(sar, branch_config_at(dl.workdate))) AS draft_used_hours
FROM payroll.payrolldraftlines dl
JOIN payroll.PayrollStatusKeys sk
    ON sk.StatusCode = dl.Notes AND sk.CompanyID = dl.CompanyID
JOIN payroll.StatusKeyAllowanceRules sar
    ON sar.StatusKeyID = sk.StatusKeyID
    AND sar.IsActive = TRUE
    AND sar.EffectiveFrom <= dl.WorkDate
    AND (sar.EffectiveTo IS NULL OR sar.EffectiveTo >= dl.WorkDate)
    AND sar.AllowanceCategoryID IS NOT NULL
WHERE dl.LineType = 'DailyStatus'
  AND dl.Status != 'Void'
  AND dl.PayrollPeriodID = :period_id
GROUP BY dl.driverid, sar.AllowanceCategoryID
```

**Balance during open period:**

```
AvailableHours = AllowedHours
              - LedgerUsedHours  (from AllowanceUsageLedger for locked periods)
              - DraftUsedHours   (derived above for open period)
```

**Enforcement:** At day-grid save, before writing the `DailyStatus` line, the service runs the balance check. If the new deduction would exhaust the entitlement: block with a descriptive error. Block vs. warn is a DAC-0 product decision.

---

## 9. Finalized Ledger Strategy

For **locked periods** (`PayrollPeriods.Status = 'Locked'`):

At finalization, after all `PayrollFinalLines` are written, the finalization service writes one `AllowanceUsageLedger` row per `DailyStatus` final line where the status key has an active `StatusKeyAllowanceRules` row with `AllowanceCategoryID IS NOT NULL`.

Key fields written:

```
EntryDirection            = 'Debit'
EntryType                 = 'Usage'
HoursAmount               = resolve_deduction_hours(rule, branch_config_at(work_date))
DeductionSource           = 'FinalizedLine'
FinalLineID               = :final_line_id
WorkDate                  = :work_date
PayrollPeriodID           = :period_id
StatusKeyID               = :status_key_id
StatusCodeSnapshot        = status_key.StatusCode       -- required
CategoryCodeSnapshot      = category.CategoryCode       -- required
DeductionAmountTypeSnapshot = rule.DeductionAmountType
HoursPerAllowanceDaySnapshot = branch_config.HoursPerAllowanceDay
AllowanceRuleRevisionSnapshot = rule.Revision
-- Optional (if included from DAC-6):
StatusKeyNameSnapshot     = status_key.KeyName
CategoryNameSnapshot      = category.CategoryName
BranchAllowanceCategoryConfigID = config.ConfigID
```

Once a period is Locked, draft-derived usage for that period is no longer queried. The ledger is authoritative.

---

## 10. Idempotency and Double-Count Protection

### 10.1 Unique constraints

```sql
-- One finalized-line ledger Usage entry per FinalLineID
CREATE UNIQUE INDEX ux_AllowanceLedger_FinalLine
    ON payroll.AllowanceUsageLedger (FinalLineID)
    WHERE FinalLineID IS NOT NULL
      AND EntryType = 'Usage'
      AND DeductionSource = 'FinalizedLine';

-- One ledger entry per AdjustmentRequest
CREATE UNIQUE INDEX ux_AllowanceLedger_AdjustmentRequest
    ON payroll.AllowanceUsageLedger (AdjustmentRequestID)
    WHERE AdjustmentRequestID IS NOT NULL;
```

### 10.2 Finalization retry safety

Finalization inserts ledger rows with `INSERT ... ON CONFLICT (FinalLineID) WHERE EntryType = 'Usage' AND DeductionSource = 'FinalizedLine' DO NOTHING`. A retry produces no duplicates.

### 10.3 Double-count protection summary

| Scenario | Protection |
|---|---|
| Day grid saved twice | No ledger writes at draft time; derivation always reads current state |
| Finalization retried | Unique index on `(FinalLineID, EntryType='Usage')` → skip on conflict |
| Adjustment approved twice | Unique index on `AdjustmentRequestID` → error on second attempt |
| Period unlock + re-finalize | Service checks for existing ledger rows; writes reversals before re-locking |
| DailyStatus line voided in open period | No ledger row exists; draft derivation excludes Void lines |

---

## 11. Cycle Strategy

### Supported initial cycle types

| Type | Description | Status |
|---|---|---|
| `Annual` | 12-month window from anchor date | Supported |
| `HalfYear` | 6-month window; two per year | Supported |
| `Quarterly` | 3-month window; four per year | Supported |
| `Custom` | User-specified month count | Future/Advanced — requires DAC-0 approval |

### Why `NineMonths` is deferred

`NineMonths` was proposed in v1 with a "9-month tracking window + 3-month gap" interpretation. This interpretation is ambiguous: it could mean a repeating 9-month cycle, a 9-month window inside a 12-month policy year, or a company-specific accrual policy. Hardcoding a specific interpretation would misrepresent policies that differ. Any company needing a 9-month cycle should use `Custom` with `CycleMonths = 9` once `Custom` is approved. `NineMonths` may be re-added as a named type in a later phase if a standard interpretation is agreed on at DAC-0.

### Cycle boundary computation

```python
def compute_cycle_boundaries(
    cycle_type: str,
    cycle_months: int | None,
    anchor_month: int,
    anchor_day: int,
    reference_date: date,
) -> tuple[date, date]:
    """
    Returns (cycle_start, cycle_end) inclusive such that reference_date falls inside.
    Uses dateutil.relativedelta for month arithmetic.
    AnchorDay capped at 28 by DB constraint — February 28 is always safe.
    """
```

Payroll periods crossing cycle boundaries: usage is evaluated per `WorkDate`, not per period. A period spanning March 28 – April 4 with an April 1 annual cycle boundary produces deductions in two separate cycle entitlements.

---

## 12. Branch Config Strategy

Effective-dated rows, never overwritten. New row inserted when configuration changes. Historical deduction rows snapshot the resolved value via `HoursPerAllowanceDaySnapshot`.

**Resolution at deduction time:**

```sql
SELECT HoursPerAllowanceDay, ConfigID
FROM payroll.BranchAllowanceCategoryConfig
WHERE AllowanceCategoryID = :cat_id
  AND BranchID = :branch_id
  AND EffectiveFrom <= :work_date
  AND (EffectiveTo IS NULL OR EffectiveTo >= :work_date)
  AND IsActive = TRUE
ORDER BY EffectiveFrom DESC
LIMIT 1
```

Falls back to a company-level default if no branch row exists. Raises if neither is configured.

No-overlap protection for this table: see §13.2.

---

## 13. Versioning, Effective-Date, and No-Overlap Strategy

### 13.1 Versioned rules table (`StatusKeyAllowanceRules`)

The allowance-relevant configuration fields (`AllowanceCategoryID`, `DeductionAmountType`, `FixedDeductionHours`, `AllowsMultiDayDeduction`) are in `StatusKeyAllowanceRules`, not as mutable columns on `PayrollStatusKeys`. A silent edit to a status key's deduction rule must not retroactively change the interpretation of past entries.

**When an admin changes the allowance deduction structure:**

1. Service closes the current active rule row: `EffectiveTo = requested_change_date - 1 day`
2. New rule row inserted: `EffectiveFrom = requested_change_date`, `Revision = previous + 1`
3. `PayrollStatusKeys.AllowanceCategoryID` updated to reflect new rule's category
4. Service validates no open `DailyStatus` draft lines for that status key exist in the affected date range; warns or blocks if so

**Historical protection:** `AllowanceRuleRevisionSnapshot` on `AllowanceUsageLedger` records which rule revision was active at deduction time. Past ledger rows are unambiguous regardless of future rule changes.

**Guard against silent structural edits:** The service must reject any UPDATE to `StatusKeyAllowanceRules` rows that have been used (i.e., where any `AllowanceUsageLedger` row references a deduction based on that rule revision). The only allowed modification to a used rule row is setting `EffectiveTo` to close it.

### 13.2 No-overlap protection for effective-dated rows

**Required invariant:** For the same logical scope, active date windows must not overlap.

- **`StatusKeyAllowanceRules`:** one active rule per `StatusKeyID` per `WorkDate`
- **`BranchAllowanceCategoryConfig`:** one active config per `(BranchID, AllowanceCategoryID)` per `WorkDate`

**Enforcement layers:**

**1. Service-level validation (required in all phases):** Before inserting or updating a rule or config row, the service queries for any existing active row whose date window intersects the new window. If an overlap is found, the insert is rejected. The service is responsible for closing the previous row (`EffectiveTo = new_EffectiveFrom - 1 day`) before opening the new one.

**2. Partial unique index on open rows (strongly recommended):**

```sql
-- For StatusKeyAllowanceRules:
CREATE UNIQUE INDEX ux_StatusKeyAllowanceRules_OneOpenRow
    ON payroll.StatusKeyAllowanceRules (StatusKeyID)
    WHERE EffectiveTo IS NULL AND IsActive = TRUE;

-- For BranchAllowanceCategoryConfig:
CREATE UNIQUE INDEX ux_BranchAllowanceCategoryConfig_OneOpenRow
    ON payroll.BranchAllowanceCategoryConfig (AllowanceCategoryID, BranchID)
    WHERE EffectiveTo IS NULL AND IsActive = TRUE;
```

Prevents the most common overlap scenario (two open-ended rows for the same scope) at the DB level.

**3. PostgreSQL exclusion constraint (optional):** A `USING GIST` exclusion constraint on `daterange(EffectiveFrom, EffectiveTo, '[)')` can be added if the `btree_gist` extension is available. This provides full closed-row overlap protection at the DB level. Not mandated, but recommended if the implementation team chooses to add it.

**Minimum required:** Service-level validation + partial unique index on open rows.

---

## 14. Status-Key Relational Link Strategy

### 14.1 Shadow column retention

`PayrollStatusKeys.AllowanceCategory VARCHAR(50)` is retained through DAC-9. It is not dropped in DAC-2 through DAC-8.

### 14.2 Shadow column synchronization — exact rule

**What the shadow column stores:** `CategoryCode` from `AllowanceCategories` (e.g. `VACATION`, `JURY_DUTY`) — not the `CategoryName` (display label). `CategoryCode` is stable; `CategoryName` is user-editable.

**CategoryCode length constraint (mandatory while shadow column exists):**

The existing DB check constraint on `PayrollStatusKeys` requires a non-null `AllowanceCategory` value for any status key where `DeductsFromYearlyAllowance = TRUE`. While this constraint exists:

- `CategoryCode` is capped at **50 characters max** on `AllowanceCategories`
- The service must **reject** creation of any category whose `CategoryCode` exceeds 50 characters
- The service must **reject** linking a deducting status key to a category with a `CategoryCode` longer than 50 characters
- The shadow column must **never be set to NULL** for a deducting key while the old check constraint exists; doing so violates the existing DB constraint

The 50-character cap is temporary technical debt tracked for removal at DAC-9 when the shadow column and its associated check constraints are dropped.

**Dual-write rule:**

When creating or updating a `StatusKeyAllowanceRules` row:
1. Validate `category.CategoryCode` length ≤ 50. Reject with a clear error if exceeded.
2. Write `AllowanceCategoryID` (canonical FK) to `PayrollStatusKeys`.
3. Synchronize `PayrollStatusKeys.AllowanceCategory` with `category.CategoryCode`.

**Hardcoded validator replacement (DAC-2, mandatory):** The Pydantic `_ALLOWANCE_CATEGORIES` set in `backend/app/settings/schemas.py` must be replaced with a company-category DB lookup against `AllowanceCategories`. Not optional.

### 14.3 DB enforcement on AllowanceCategoryID

```sql
ALTER TABLE payroll.PayrollStatusKeys
  ADD CONSTRAINT ck_StatusKeys_AllowanceFkConsistency
  CHECK (
    (DeductsFromYearlyAllowance = FALSE AND AllowanceCategoryID IS NULL)
    OR
    (DeductsFromYearlyAllowance = TRUE  AND AllowanceCategoryID IS NOT NULL)
  );
```

Additional service rules:
- `AllowanceCategoryID` must belong to the same `CompanyID` as the status key
- An inactive `AllowanceCategories` row cannot be selected for a new rule
- `EnteredHours` deduction type returns 422 until explicitly implemented: `"EnteredHours deduction type is not yet supported."`

---

## 15. Driver Entitlement Strategy

- **Entitlement creation:** Manual in DAC-4. Auto-provisioning is a future DAC-7+ feature.
- **Missing entitlement handling:** Block — return 422 with descriptive message. No silent auto-create.
- **One entitlement per driver-category-cycle:** `UNIQUE (DriverID, AllowanceCategoryID, CycleStart)` enforced at DB level.
- **Overage behavior:** Block by default with a clear message. A future `AllowOverage` flag on the entitlement or category level can permit overages with a warning. Default confirmed at DAC-0.

---

## 16. Adjustment / Reset Governance

### Approval rules

| Type | Who requests | Who approves | Self-approve? |
|---|---|---|---|
| `AdjustHours` | Branch manager or company admin | Company admin | Only if requester is company admin |
| `CorrectEntry` | Branch manager or company admin | Company admin | Only if requester is company admin |
| `ResetCycle` | Company admin only | Second company admin (recommended) or same admin if policy allows | Must be confirmed; not automatic |

For high-impact operations (`ResetCycle`, `CorrectEntry` for finalized periods), a different company-level reviewer may be required. This is a DAC-0 product decision. The `ReviewedByUserID` field supports enforcement.

### Reuse of existing patterns

`AllowanceAdjustmentRequests` is domain-specific and must remain its own table. It should reuse:
- The same `audit.AuditLog` entity logging pattern used for other settings mutations
- The same company-scoped permission validation pattern
- The same approval notification infrastructure if it exists

### What approval writes

- **`AdjustHours`:** Updates `DriverAllowanceEntitlements.AllowedHours`; writes audit log snapshot. No ledger row.
- **`CorrectEntry`:** Writes a `Credit / Reversal` ledger row for `HoursAmount = original debit HoursAmount`.
- **`ResetCycle`:** Writes a `Credit / Reset` ledger row for `HoursAmount = current_net_used_hours`. Original rows unchanged.

### Guards

- Blank `Reason` blocked at DB level
- `EffectiveDate` inside a Locked period requires `force_locked_period = TRUE` plus non-blank `ReviewNotes`
- Adjustment cannot produce a negative balance; service validates before writing

---

## 17. Migration / Backfill Strategy

### DAC-1 — Create AllowanceCategories

1. Create `payroll.AllowanceCategories` table (including `UNIQUE (CompanyID, AllowanceCategoryID)`)
2. Seed six rows per existing company: `VACATION`, `SICK`, `BEREAVEMENT`, `JURY_DUTY`, `PERSONAL`, `OTHER`
3. Expose read-only `GET /settings/allowance-categories`

### DAC-2 — Relational link + rules table + shadow sync

1. Create `payroll.StatusKeyAllowanceRules` table
2. Add `AllowanceCategoryID BIGINT NULL FK` to `PayrollStatusKeys`
3. Add `UNIQUE (CompanyID, StatusKeyID)` to `PayrollStatusKeys`
4. Verify `UNIQUE (CompanyID, BranchID)` and `UNIQUE (CompanyID, DriverID)` exist on branches and drivers tables; add if not present (check for duplicate pairs before applying)
5. Backfill `AllowanceCategoryID`:

```sql
UPDATE payroll.PayrollStatusKeys sk
SET AllowanceCategoryID = (
    SELECT ac.AllowanceCategoryID FROM payroll.AllowanceCategories ac
    WHERE ac.CompanyID = sk.CompanyID
      AND ac.CategoryCode = CASE sk.AllowanceCategory
            WHEN 'Vacation'    THEN 'VACATION'
            WHEN 'Sick'        THEN 'SICK'
            WHEN 'Bereavement' THEN 'BEREAVEMENT'
            WHEN 'Jury Duty'   THEN 'JURY_DUTY'
            WHEN 'Personal'    THEN 'PERSONAL'
            WHEN 'Other'       THEN 'OTHER'
            ELSE NULL END
)
WHERE sk.DeductsFromYearlyAllowance = TRUE AND sk.AllowanceCategory IS NOT NULL;
```

6. Validate: every `DeductsFromYearlyAllowance = TRUE` row has non-null `AllowanceCategoryID`. Fail migration on any gap.
7. Validate: all mapped `CategoryCode` values are ≤ 50 characters (trivially true for seeded categories; confirm before enforcing cap).
8. Seed one `StatusKeyAllowanceRules` row per backfilled status key (Revision=1, EffectiveFrom=earliest meaningful date, EffectiveTo=NULL).
9. Update application to dual-write `AllowanceCategoryID` + `AllowanceCategory` shadow (as `CategoryCode`).
10. Replace Pydantic `_ALLOWANCE_CATEGORIES` hardcoded validator with company-category DB lookup.
11. **Old `AllowanceCategory VARCHAR(50)` column is NOT dropped.**

### DAC-8 — Historical backfill and pre-DAC marking

Backfill snapshot columns on `PayrollFinalLines`:

```sql
UPDATE payroll.PayrollFinalLines fl
SET
    AllowanceCategoryID        = sk.AllowanceCategoryID,
    AllowanceCategorySnapshot  = ac.CategoryCode,
    DeductionSource            = 'PreDacInferred'
FROM payroll.PayrollStatusKeys sk
JOIN payroll.AllowanceCategories ac
    ON ac.AllowanceCategoryID = sk.AllowanceCategoryID
WHERE fl.LineType = 'DailyStatus'
  AND fl.Notes = sk.StatusCode
  AND sk.CompanyID = fl.CompanyID
  AND sk.DeductsFromYearlyAllowance = TRUE
  AND sk.AllowanceCategoryID IS NOT NULL;

UPDATE payroll.PayrollFinalLines fl
SET DeductionSource = 'PreDacUnknown'
WHERE fl.LineType = 'DailyStatus'
  AND fl.AllowanceCategoryID IS NULL;
```

`PreDacInferred` = matched using current mapping; not guaranteed historically accurate if the key was re-linked before DAC-2. Reports must visually distinguish `PreDacInferred` rows from `FinalizedLine` rows.

### DAC-9 — Retire old text column

Only after:
- Relational links proven in production
- All reports validated against `AllowanceCategoryID`
- All API consumers confirmed to use `AllowanceCategoryID`
- No remaining code references `AllowanceCategory` text column
- CategoryCode 50-character cap removed from `AllowanceCategories`

Steps: drop dual-write → migration to `DROP COLUMN AllowanceCategory` → drop old text-based CHECK constraints.

---

## 18. Tenant Integrity and Permission / Security Model

### 18.1 Principle

Service-layer guards are required and are the first line of defense. DB-level tenant integrity is also required wherever feasible. These layers are complementary; neither replaces the other.

The DB cannot guarantee cross-table consistency by `CompanyID` alone unless composite unique constraints or composite FKs are in place. Where tables do not yet expose the composite unique keys needed to reference them cross-company, those constraints must be added before the composite FK can be created.

### 18.2 Composite FK requirements — full coverage

**`AllowanceCategories`**
- `UNIQUE (CompanyID, AllowanceCategoryID)` — specified in §4.1
- Referenced as composite FK target by: `BranchAllowanceCategoryConfig`, `AllowanceCycles`, `DriverAllowanceEntitlements`, `AllowanceUsageLedger`, `AllowanceAdjustmentRequests`, `StatusKeyAllowanceRules`

**`PayrollStatusKeys`**
- Add `UNIQUE (CompanyID, StatusKeyID)` in DAC-2
- Referenced as composite FK target by: `StatusKeyAllowanceRules`

**Branches table**
- Requires `UNIQUE (CompanyID, BranchID)` — must be confirmed from migration files; add if not present
- Referenced as composite FK target by: `BranchAllowanceCategoryConfig`, `DriverAllowanceEntitlements`, `AllowanceUsageLedger`, `AllowanceAdjustmentRequests`

**Drivers table**
- Requires `UNIQUE (CompanyID, DriverID)` — must be confirmed from migration files; add if not present
- Referenced as composite FK target by: `DriverAllowanceEntitlements`, `AllowanceUsageLedger`, `AllowanceAdjustmentRequests`

**`PayrollFinalLines` / `PayrollDraftLines`**
- Composite FK on `(CompanyID, FinalLineID)` is impractical without adding `UNIQUE (CompanyID, FinalLineID)` to a large table
- **Alternative:** Simple FK + mandatory service-layer check that the final/draft line's `CompanyID` matches the ledger row's `CompanyID` before writing. Service check is authoritative here.

**`PayrollPeriodID` references**
- No direct FK on `AllowanceUsageLedger`; stored for traceability
- Service must verify the period's `CompanyID` matches before writing the ledger row

**`DriverAllowanceEntitlements`**
- Referenced from `AllowanceUsageLedger` and `AllowanceAdjustmentRequests` by simple FK
- The entitlement row's `CompanyID` verified by the service before writing

**Summary of DB-level composite FK coverage:**

| Table | Composite FKs provided | Service check also required |
|---|---|---|
| `BranchAllowanceCategoryConfig` | `(CompanyID, AllowanceCategoryID)`, `(CompanyID, BranchID)` | Yes |
| `DriverAllowanceEntitlements` | `(CompanyID, AllowanceCategoryID)`, `(CompanyID, BranchID)`, `(CompanyID, DriverID)` | Yes |
| `AllowanceUsageLedger` | `(CompanyID, AllowanceCategoryID)`, `(CompanyID, DriverID)`, `(CompanyID, BranchID)` | Yes (for FinalLineID, DraftLineID, PayrollPeriodID) |
| `AllowanceAdjustmentRequests` | `(CompanyID, AllowanceCategoryID)`, `(CompanyID, BranchID)`, `(CompanyID, DriverID)` | Yes |
| `StatusKeyAllowanceRules` | `(CompanyID, AllowanceCategoryID)`, `(CompanyID, StatusKeyID)` | Yes |

### 18.3 Service-layer enforcement contract

1. `company_id` is always derived from the JWT token — never from the request body for ownership checks
2. Every query against `AllowanceCategories`, `DriverAllowanceEntitlements`, and `AllowanceUsageLedger` filters by `company_id` from token
3. Branch-scoped queries additionally join through branch membership validation
4. Before writing any ledger row referencing `FinalLineID`, `DraftLineID`, or `PayrollPeriodID`, the service verifies the source row's `CompanyID` matches the token's `CompanyID`
5. `AllowanceCategoryID` must belong to the same `CompanyID` as the status key; verified via join
6. An inactive `AllowanceCategories` row cannot be selected for a new status key rule

### 18.4 Permission summary

| Action | Company admin | Branch manager | Branch user |
|---|---|---|---|
| Create/edit `AllowanceCategories` | ✅ | ❌ | ❌ |
| View `AllowanceCategories` | ✅ | ✅ (read) | ✅ (read) |
| Set `BranchAllowanceCategoryConfig` | ✅ | ✅ (own branch) | ❌ |
| Create `DriverAllowanceEntitlements` | ✅ | ✅ (own branch drivers) | ❌ |
| View entitlements/balances | ✅ (all branches) | ✅ (own branch) | ❌ |
| Submit `AllowanceAdjustmentRequests` | ✅ | ✅ | ❌ |
| Approve `AllowanceAdjustmentRequests` | ✅ | ❌ | ❌ |
| View `AllowanceUsageLedger` | ✅ (all branches) | ✅ (own branch) | ❌ |

---

## 19. Risks and Tradeoffs

### R1 — Draft-derived usage adds latency to day-grid saves

The derivation query runs in the same transaction as the day-grid save. Mitigation: index `PayrollDraftLines` on `(PayrollPeriodID, DriverID, LineType, Status)` and cache rule resolution within the save transaction.

### R2 — Payroll periods crossing cycle boundaries require per-date evaluation

Each `WorkDate` in a multi-day save is evaluated independently. `compute_cycle_boundaries()` must be memoized within the save transaction. A dedicated test suite for boundary computation is mandatory before DAC-5 ships.

### R3 — `EnteredHours` deduction type is architecturally incomplete

`DailyStatus` lines carry `Quantity = 0`. No mechanism exists for entering a per-status quantity. The 422 guard is mandatory until the capability is explicitly built.

### R4 — `PreDacInferred` backfill is not guaranteed historically accurate

The backfill maps pre-existing final lines to categories based on the current status key linkage. If a status key was re-linked between the original entry and the backfill, the inferred mapping will be wrong. Reports must visually distinguish `PreDacInferred` rows.

### R5 — CategoryCode length cap while shadow column exists

`CategoryCode` is capped at 50 characters while `PayrollStatusKeys.AllowanceCategory VARCHAR(50)` exists. This is a hard enforcement rule. The service must reject creation of any category whose `CategoryCode` exceeds 50 characters until DAC-9. Tracked as technical debt cleared at DAC-9.

### R6 — `StatusKeyAllowanceRules` versioning adds complexity to day-grid save path

In DAC-5, the save path must look up the active rule per `WorkDate` for each deducting status key. Mitigation: index on `(StatusKeyID, EffectiveFrom)` and pre-fetch all active rules for the period's date range before iterating lines.

### R7 — No auto-provisioning of entitlements before DAC-7

Manual entitlement creation for all active drivers at DAC-4 launch is a setup burden. Auto-provisioning is a DAC-7+ feature. Stakeholder expectations must be managed.

### R8 — DAC-5 is user-visible, feature-flagged, and high-risk

DAC-5 modifies the day-grid save path — the most sensitive write path in the system. A bug in DAC-5 can block payroll entry for all users. Required before releasing DAC-5:
- Feature flag to enable/disable enforcement
- Focused regression tests covering the entire day-grid save flow
- Existing usage-limit enforcement (`_enforce_status_key_limits()`) must not be broken
- Explicit rollback plan

### R9 — NineMonths deferral may require re-opening cycle design

Minor schema change to add it as a named type in a later phase if a standard interpretation is agreed at DAC-0.

### R10 — Composite unique constraints required on existing tables

Adding `UNIQUE (CompanyID, BranchID)` on branches, `UNIQUE (CompanyID, DriverID)` on drivers, and `UNIQUE (CompanyID, StatusKeyID)` on `PayrollStatusKeys` requires verification that no duplicate pairs exist in production data. This check must happen in DAC-2 migration planning.

---

## 20. Future Phase Plan

> **This plan is not active. Implementation should begin only after a dedicated DAC phase is explicitly approved.**

| Phase | Name | Schema changes | Code changes | User-visible? | Risk |
|---|---|---|---|---|---|
| **DAC-0** | Product decisions finalized | None | None | No | — |
| **DAC-1** | AllowanceCategories catalog | `AllowanceCategories` table; seed defaults per company | Read-only `GET` endpoints | No | Low |
| **DAC-2** | Status Key relational link + rules table | `StatusKeyAllowanceRules`; `AllowanceCategoryID FK` on `PayrollStatusKeys`; `UNIQUE (CompanyID, StatusKeyID)` on `PayrollStatusKeys`; composite unique on branches/drivers if missing | Backfill; dual-write; DB-lookup validator; rule versioning enforcement; CategoryCode ≤ 50 enforcement; no-overlap service validation | Partially (settings UI: category dropdown) | Medium |
| **DAC-3** | Effective-dated branch config | `BranchAllowanceCategoryConfig`; no-overlap partial unique index | Branch config CRUD; resolution function; no-overlap service validation | No | Low |
| **DAC-4** | Driver entitlements + cycle | `AllowanceCycles`; `DriverAllowanceEntitlements` | `compute_cycle_boundaries()` with full edge-case tests; entitlement CRUD | No | Low |
| **DAC-5** | Open-period usage preview + enforcement | None | Draft usage derivation; balance check in day-grid save; overage behavior per DAC-0 decision | **Yes — user-visible. Blocks saves when balance exceeded. Feature-flagged. High-risk. Focused regression tests required.** | **High** |
| **DAC-6** | Immutable finalized ledger | `AllowanceUsageLedger` with idempotency indexes; snapshot columns on `PayrollFinalLines`; optional audit snapshots if practical | Finalization ledger writes; `ON CONFLICT DO NOTHING`; balance queries use ledger for locked periods | No | Medium |
| **DAC-7** | Adjustment/reset governance | `AllowanceAdjustmentRequests` | Approval flow; ledger entry on approval; locked-period guards; audit log integration | Yes (admin-facing) | Medium |
| **DAC-8** | Historical backfill + pre-DAC marking | None | Backfill `DailyStatus` final lines; `PreDacInferred`/`PreDacUnknown` marking; audit snapshot columns if deferred from DAC-6 | No | Low |
| **DAC-9** | Retire old text column | Drop `AllowanceCategory VARCHAR(50)` from `PayrollStatusKeys`; drop associated CHECK constraints; lift CategoryCode 50-char cap | Drop dual-write; validator to FK-only | No | Low |
| **DAC-10** | Frontend Driver Center | None | Allowance balance dashboard; entitlement management; adjustment submission/review; history per driver | **Yes — first fully user-facing phase** | Medium |

---

## 21. Files and Code Areas Considered

| File / Area | What was examined |
|---|---|
| `backend/app/settings/schemas.py` | `_ALLOWANCE_CATEGORIES` hardcoded set (lines 27–29); `StatusKeyCreate` with `allowance_category` and `category_valid` validator; `DeductsFromYearlyAllowance` field |
| `backend/app/settings/service.py` | `_validate_deduction_rules()`; Status Key create/update paths |
| `backend/app/payroll/service.py` | DailyStatus line INSERT (~lines 9067–9085): `LineType='DailyStatus'`, `Quantity=0`, `Notes=status_code`, `HoursValue` not applied; `_enforce_status_key_limits()` (~lines 8261–8454): separate from allowance; finalization INSERT (~lines 2910–3047): `DailyStatus` copies with `FinalAmount=0`; `DayGridStatusKey` response (~line 8644): `HoursValue` for UI only |
| `backend/alembic/versions/0001_initial_schema.sql` | `PayrollStatusKeys` (lines 359–380); `PayrollDraftLines` (line 382); `PayrollFinalLines` (line 408); `AllowanceCategory` CHECK constraints (lines 1204–1218) |
| `backend/alembic/versions/0003_pay_items_seed.sql` | Confirmed: `PTO_STATUS` is a seeded `PayItems` row; it is a pay item, not the Day Grid Status Entry Channel |
| `backend/alembic/versions/0018_status_keys_key_name.sql` | `KeyName`, 8 usage-limit columns, DB CHECK constraints |
| `backend/alembic/versions/0034_final_line_source_snapshot.sql` | Source tracking columns on `PayrollFinalLines` |
| `backend/alembic/versions/0039_source_snapshot_and_rate_mutation_guard.sql` | `SourceSnapshot JSONB` on `PayrollFinalLines` |
| Code-wide grep for `AllowanceCategory`, `DeductsFromYearlyAllowance` in `backend/app/**/*.py` | Zero matches — confirmed not used in any Python service or router beyond settings schema validation |

---

## 22. Final Verdict

**`Revised allowance architecture v4 ready to save as future MD plan`**

All Codex-requested corrections from three review passes are incorporated and accepted:

| Correction | Status |
|---|---|
| Status is a System Status Entry Channel, not a PayItem | ✅ Accepted |
| PTO_STATUS is a separate seeded pay item; not the Day Grid Status Entry Channel | ✅ Clarified |
| StatusKeyPayRule reserved as a future separate concept | ✅ Accepted |
| NineMonths deferred | ✅ Accepted |
| Open-period usage derived from draft lines | ✅ Accepted |
| Ledger uses positive HoursAmount + EntryDirection | ✅ Accepted |
| Effective-dated tables require no-overlap protection | ✅ Added in v4 |
| Tenant integrity requires DB-level composite FKs for all FK dimensions | ✅ Added in v4 |
| Ledger snapshot nullability fixed for adjustment/reset rows | ✅ Fixed in v4 |
| Shadow column NULL relaxation removed; CategoryCode ≤ 50 enforced | ✅ Fixed in v4 |
| Schema names corrected to project's actual schemas | ✅ Fixed in v4 |
| Optional audit snapshots kept as non-blocking | ✅ Preserved |
| DAC-5 is user-visible, feature-flagged, and high-risk | ✅ Accepted |
| Frontend Driver Center is future-only | ✅ Accepted |

---

*Document generated: 2026-06-18 · Branch: custom-daily-refactor · Not an active implementation task.*
